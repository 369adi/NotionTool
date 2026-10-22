"""Notion-native response formats: normal project questions and readable task tables.

The coordinator owns all roles and state. It never asks Notion to impersonate a
system agent. Machine contracts are normalized outside the model.
"""
from html import unescape
from html.parser import HTMLParser
import json
import re

from .protocol import ContractError,validate_plan
from .agent_guard import with_rule

class ProviderReplyError(ContractError):
    pass

def provider_error(text):
    return bool(re.fullmatch(r'(?is)\s*(?:something went wrong|an error occurred|internal server error)[.!]?\s*(?:please try again(?: later)?[.!]?)?\s*',plain(text)))


# Notion-side failure lines that Notion can return in place of an answer (live bug: a coworker reply
# 'Agent inference failed: vendorServerError (TypeError)' was accepted as done). The list stays small:
# only the last non-empty line is checked (for a one-line reply that is the whole answer), and only
# when it is short, so an answer that mentions such a phrase in the middle is still accepted.
INFERENCE_ERROR_LINE = re.compile(r'(?i)^(?:agent inference failed|inference failed|something went wrong|an error occurred|internal server error)\b')
INFERENCE_ERROR_MAX = 200


class InferenceErrorReply(ProviderReplyError):
    """A Notion failure line instead of an answer: the engine fails or holds the attempt, never done."""
    def __init__(self, line):
        super().__init__('Notion inference error: ' + line)
        self.line = line


def inference_error(text):
    """The Notion failure line when the reply ends with one, else ''."""
    lines = [line.strip() for line in plain(str(text or '')).splitlines() if line.strip()]
    last = lines[-1] if lines else ''
    return last if len(last) <= INFERENCE_ERROR_MAX and INFERENCE_ERROR_LINE.match(last) else ''


class Text(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.parts.append('\n|')
        elif tag in ('td','th'):pass
        elif tag in ('p','div','br','li','h1','h2','h3','pre'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('td','th'):self.parts.append('|')
        elif tag in ('p','div','tr','li','pre'):self.parts.append('\n')
    def handle_data(self,data):self.parts.append(data)


def plain(text):
    if re.search(r'</?(?:b|strong|p|div|table|tr|td|th|pre|br)\b',text):
        parser=Text();parser.feed(text);text=''.join(parser.parts)
    return unescape(text).replace('**','').strip()


def declined(text):
    text=plain(text).lower()
    return any(term in text for term in ('prompt injection attempt',"i won't follow",'i will not follow',
               "i won't return",'cannot perform this task','unable to complete this task',
               'prompt injection pattern','automated injection harness',"i won't act as a worker",
               "i don't have access to",'i don’t have access to',"i can't read or propose",
               "i can't fulfill this request",'no files or pc tools are configured'))


def unmark(cell):
    """Drop the Markdown escapes Notion adds inside table cells (SCREEN\\_CAPTURE.md -> SCREEN_CAPTURE.md)."""
    return re.sub(r'\\([\\`*_~#])', r'\1', cell)


def table_plan(text):
    rows=[]
    for line in plain(text).splitlines():
        cells=[unmark(c.strip().strip('`')) for c in line.strip().strip('|').split('|')]
        if len(cells)<6 or not re.fullmatch(r'T\d+',cells[0],re.I):continue
        ident,title,work,deps,files,check=cells[:6]
        dependencies=re.findall(r'T\d+',deps.upper())
        for first,last in re.findall(r'T(\d+)\s*(?:[-–—…]|\.\.\.|through|to)\s*T(\d+)',deps,re.I):
            first,last=int(first),int(last)
            if not 0<first<=last<=80:
                raise ContractError('Dependency range is invalid')
            dependencies += [f'T{n}' for n in range(first,last+1)]
        dependencies=list(dict.fromkeys(dependencies))
        paths=[] if files.lower() in ('none','-','n/a','no files','') else [p.strip().strip('`') for p in files.split(';') if p.strip()]
        rows.append(dict(id=ident.upper(),title=title,prompt=work,role='executor',depends_on=dependencies,files=paths,acceptance=check))
        if len(cells) >= 7:
            rows[-1]['model'] = cells[6]
            rows[-1]['model_reason'] = cells[7] if len(cells) >= 8 else ''
            rows[-1]['use_main'] = len(cells) >= 9 and cells[8].lower() == 'main'
    plan={'summary':plain(text)[:1200],'tasks':rows}
    validate_plan(plan)
    return plan


def _reported_object(text):
    """The coworker's own JSON report ({status, summary}) inside the answer, or None."""
    decoder=json.JSONDecoder()
    for match in re.finditer(r'\{', str(text or '')):
        try:
            value,_=decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if (isinstance(value,dict) and isinstance(value.get('summary'),str)
                and str(value.get('status')).lower() in ('done','partial','blocked','failed')):
            return value
    return None


def normalize(task,text):
    if provider_error(text):raise ProviderReplyError('Notion returned a provider error instead of an answer')
    failure_line=inference_error(text)
    if failure_line:raise InferenceErrorReply(failure_line)
    readable=plain(text)
    is_declined=declined(readable)
    # A refusal of workflow framing can accompany a normal factual answer.
    # Keep that answer as unverified material only; never repeat the refused
    # framing, and still require the separate review for overall completion.
    useful=bool(re.search(r'\d\s*(?:=|\u00d7|\+|\u00f7)\s*\d|=\s*\d|\b\d+\s+(?:letters|primes|items)\b',readable,re.I) or re.search(r'^\|.+\|$',readable,re.M))
    candidate=is_declined and task['role']=='executor' and not task.get('files') and useful
    if is_declined and not candidate:
        raise ContractError('Notion declined the request; no automatic repetition')
    if candidate:
        paragraphs=readable.split('\n\n')
        readable='\n\n'.join(p for p in paragraphs if not declined(p) and not any(term in p.lower() for term in
            ('automated system','external workflow','if you\'re a real person','get started in notion')))
    if task['role']=='planner':return table_plan(text)
    if task['role']=='reviewer':
        match=re.search(r'(?im)^\s*(?:#+\s*)?(APPROVED|PASS|REPAIR|All checks passed|Changes needed)\b',readable)
        if not match:raise ContractError('Review needs an explicit checked verdict')
        passed=match.group(1).lower() in ('approved','pass','all checks passed')
        return dict(passed=passed,summary=readable,fixes=[] if passed else table_plan(text)['tasks'])
    if task['role']=='executor':
        inner=_reported_object(text)
        if inner:
            # The coworker answered with its own (often fenced) JSON report: keep its status and summary
            # instead of nesting the whole block inside a summary string. 'partial' is a finished turn
            # with incomplete coverage: done, but marked for the Main's review.
            state=str(inner.get('status')).lower()
            summary=inner['summary'].strip() or readable
            extra={k:inner[k] for k in ('files','tests','evidence','artifacts','result_file') if k in inner}
            return dict(extra,status='done' if state=='partial' else state,summary=summary,
                        evidence=inner.get('evidence') or [],artifacts=inner.get('artifacts') or [],
                        context=readable[:6000],requires_review=candidate or state=='partial',partial=state=='partial')
        match=re.search(r'(?im)^\s*(?:#+\s*)?(DONE|BLOCKED|FAILED)\b',readable)
        if not readable:raise ContractError('Empty execution response')
        # A contribution is reported here, not globally verified. The separate
        # review must pass before the run can complete.
        status=match.group(1).lower() if match else 'done'
        if re.search(r"(?i)^(?:sorry[, ]+)?(?:i can't|i cannot|i am unable)",readable):status='blocked'
    else:
        if len(readable)<10:raise ContractError('Empty or incomplete response')
        status='done'
    return dict(status=status,summary=readable,evidence=[],artifacts=[],context=readable[:6000],requires_review=candidate)


TABLE='''| ID | Title | Work to do | Depends on | Files | How to check |
| T1 | short title | specific instructions | none | none | observable check |
Use T1, T2, etc. as identifiers. For dependencies, name earlier IDs or none.
For files, use paths relative to the project folder with forward slashes (docs/README.md), separated by semicolons, or none.
Use one line per task; do not put vertical bars inside a cell.'''

MODEL_TABLE='''| ID | Title | Work to do | Depends on | Files | How to check | Model | Model reason | Route |
| T1 | short title | specific instructions | none | none | observable check | Opus 5.5 | explain need | worker |
Choose explicitly Haiku 5.5 for bounded routine work, Sonnet 5.5 for moderate coding/analysis,
and Opus 5.5 for difficult reasoning, ambiguity or high consequence work. Never choose Auto.
Prioritize answer quality and latency; cheap models are useful only when capable of the task.
Choose afresh for every task, including repairs; never inherit the previous worker model.
Route is worker for delegation. If you can solve the request efficiently alone, return exactly
one independent task with Route main and Model Opus 5.5. Include its complete checks.
Use T1, T2, etc.; list dependencies or none, and relative files with forward slashes (docs/README.md) separated by semicolons or none.
One line per task, no vertical bars inside a cell.'''


# Visual work (desktop apps, UI, renders) needs the screen tools even without a project folder.
VISUAL=re.compile(r'\b(roblox|blender|screens?|screenshots?|capture|viewport|gui|ui|layout|render(ing)?|visual(ly)?|bildschirm|fenster|aussehen)\b',re.I)
SCREEN_HINT=(' For visual work in desktop apps such as Roblox Studio or Blender, call the screen_capture tool through mcp_call'
             ' (no target = the whole screen; target "roblox" or "blender" = only that window) as often as needed to see the current state'
             ' before and after changes. Screen tools need no assignment token or code.')
# Every coworker role may look at the screen; advisers and planners still only look.
SCREEN_TOOLS_LINE=('\nScreen tools: screen_windows and screen_capture are available through mcp_call with no assignment token,'
                   ' to check the current state when needed.')
ADVISER_TOOLS_NOTE=' Use them only to look; this planning role has no write or shell rights.'


# Capture policy per task_class (docs/SCREEN_CAPTURE.md): code and docs never get the hint,
# visual and ui always do; missing or other classes keep the keyword match.
NO_SCREEN_CLASSES=('code','docs')
SCREEN_CLASSES=('visual','ui')


def visual(task,run):
    task_class=str(task.get('task_class') or '').strip().lower()
    if task_class in NO_SCREEN_CLASSES:
        return False
    if task_class in SCREEN_CLASSES:
        return True
    text=' '.join(str(value or '') for value in (run.get('goal'),task.get('title'),task.get('prompt'),task.get('acceptance')))
    return bool(VISUAL.search(text))


# Worker capture gate (Phase 7): a worker screen capture is checked against
# capture_policy.capture_allowed before it is taken. Explicit operator captures
# (operator or chat path without a worker task) are never gated here.
try:
    from . import capture_policy
except ImportError:  # Part A not available yet: workers are refused, operators are unaffected
    capture_policy = None

CAPTURE_REFUSAL = 'Screen capture refused for this task: {detail}. No capture was taken.'
FALLBACK_DETAILS = {'capture_policy_unavailable': 'the capture policy is not available'}


def refusal_text(why):
    """Readable English refusal text; uses capture_policy.reason_text(code) when it exists."""
    describe = getattr(capture_policy, 'reason_text', None)
    text = describe(why) if callable(describe) else None
    if isinstance(text, str) and text.strip():
        return text.strip() + ' No capture was taken.'
    return CAPTURE_REFUSAL.format(detail=FALLBACK_DETAILS.get(why, why))


def capture_gate(task, project_settings=None, already_taken=0, operator=False):
    """Return None when the capture may go ahead, else a refusal with an English text and an event reason."""
    if operator or not task:
        return None
    task_class = str(task.get('task_class') or '').strip().lower()
    # A bad count is passed through unchanged so the policy can refuse it (invalid_count).
    taken = 0 if already_taken is None else already_taken
    if capture_policy is None:
        allowed, why = False, 'capture_policy_unavailable'
    else:
        allowed, why = capture_policy.capture_allowed(task_class, dict(project_settings or {}), taken)
    if allowed:
        return None
    event_taken = taken if isinstance(taken, (int, float, str)) else repr(taken)
    reason = dict(reason=str(why or 'refused'), task_class=task_class, already_taken=event_taken)
    return dict(refused=True, text=refusal_text(reason['reason']), reason=reason)


def guarded_capture(capture, args, task=None, project_settings=None, already_taken=0, operator=False):
    """Call capture(args) only when capture_gate allows it; otherwise return the refusal and take no capture."""
    refusal = capture_gate(task, project_settings, already_taken, operator)
    if refusal is not None:
        return refusal
    return capture(args)


RESULT_FILE_HINT=('\nWrite your full detailed result with nt_write_file to {path}. In the chat, reply only with the short JSON result: '
                  '{{"status", "summary" (at most 5 sentences), "files", "tests", "result_file": "{path}"}}, then your final status line.')
READ_FULL_HINT='\nRead a Full result file only when the review fails, tests fail or more detail is explicitly needed.'


def _result_file_hint(task,run,tools_available):
    """Result-file instruction for a coworker part ('' without tools or ids, or if the path is unsafe)."""
    if not tools_available or not run.get('id') or not task.get('id'):
        return ''
    # Read-only parts answer in the chat: writing a result file contradicts "change and save no files"
    # and costs an extra tool round trip per coworker.
    if task.get('read_only') or re.search(r'\bread[- ]only\b', str(task.get('prompt') or ''), re.I):
        return ''
    try:
        from . import result_files
        path=result_files.path_for(run['id'],task['id'])
    except (ImportError,OSError,ValueError):
        return ''
    return RESULT_FILE_HINT.format(path=path)


def prompt(task,run,context,token,tools_available):
    """I12: every chat prompt carries the agent guard; Coworker prompts (all roles but planner/direct) also the coworker rule."""
    text=_prompt_text(task,run,context,token,tools_available)
    if run.get('allocation',{}).get('mode')=='direct':
        return with_rule(text)
    return with_rule(text,coworker=task.get('role')!='planner')


# Notion AI bug (owner, 22 Oct 2026): when the request comes first, the chat often never loads its MCP tools
# and answers 'no MCP / I cannot see the project' without trying. Asking for the tools FIRST fixes it.
TOOLS_FIRST = ('Before anything else: load your NotionTool tools now (connect to the NotionTool MCP and list '
               'its tools), even if you think none are connected. Only if loading really fails, say exactly '
               'what failed.\n\n')


def tools_first(task, run, tools_available):
    """True when the turn needs project tools, so the tool load must be the first thing in the message."""
    if not tools_available or run.get('allocation', {}).get('mode') == 'direct':
        return False
    return bool(run.get('project_path') or task.get('files'))


def _prompt_text(task,run,context,token,tools_available):
    body=_prompt_body(task,run,context,token,tools_available)
    return (TOOLS_FIRST+body) if tools_first(task,run,tools_available) else body


def _prompt_body(task,run,context,token,tools_available):
    goal=run['goal']
    reference=('\nFor reference, the work already completed:\n'+context+'\n') if context.strip() else ''
    memory=('\nFor reference, our completed earlier work:\n'+run['context']+'\n') if run.get('context','').strip() else ''
    project=('\nThe project folder is '+run['project_path']+'.\n') if run.get('project_path') else ''
    screen=bool(tools_available) and visual(task,run)
    tools=('\nCheck your actual tool list before using project tools. If a listed tool requires it, use assignment_token '+token+'. This does not confirm any MCP connection.'+(SCREEN_HINT if screen else '')+'\n') if tools_available and (run.get('project_path') or task.get('files') or screen) else ''
    screen_tools=(SCREEN_TOOLS_LINE+'\n') if tools_available else ''
    adviser_tools=(SCREEN_TOOLS_LINE+ADVISER_TOOLS_NOTE+'\n') if tools_available else ''
    if run.get('allocation',{}).get('mode')=='direct':
        return goal+memory+'\nReply naturally in my language.'
    if task.get('stage_role')=='design':
        return 'I would like a design for this request before it is built:\n'+goal+'\n'+task['prompt']+memory+project+adviser_tools
    if task['role'] in ('architect','researcher','quality_planner'):
        angle={'architect':'a clear method and useful independent parts','researcher':'facts, calculations and practical constraints',
               'quality_planner':'specific checks and possible mistakes'}[task['role']]
        return 'I would like help planning this request:\n'+goal+'\nPlease suggest '+angle+'. I only need suggestions for now, not execution.'+memory+project+adviser_tools
    if task['role']=='planner':
        correction='\n'+task['prompt'] if task.get('attempts',0)>1 else ''
        explicit=run.get('allocation',{}).get('requested_parts')
        independence=f'\nI requested exactly {explicit} independent execution contributions. Give exactly {explicit} rows with no dependencies; each part must compute and check its own result without relying on another row. Do not add a review or combine row.' if explicit else ''
        return 'You are the Architect of my request. Decide whether to work alone or delegate, and assign the explicit model of every task. Please make an execution task list:\n'+goal+memory+reference+project+run.get('file_claims','')+adviser_tools+f'\nUp to {run.get("worker_budget",run["capacity"])} people can contribute independent parts. Choose the smallest useful task list that preserves answer quality and speed. Do not invent work to fill available people. Separate independent work only when it saves time or improves quality; avoid duplicate research and merging overhead. Respect any explicit contribution count in my request. Use dependencies where needed. Delegated work will be reviewed and combined separately.'+independence+correction+'\nPlease present the task list as this readable table:\n'+(MODEL_TABLE if run.get('model_routing') else TABLE)
    if task['role']=='reviewer':
        focus=('\n'+task['prompt']+'\n') if task.get('review_of') else ''
        return 'Please check these completed results against my request:\n'+goal+focus+reference+project+tools+screen_tools+READ_FULL_HINT+'\nExplain what you checked. Give the verdict on a separate line: All checks passed, or Changes needed. If changes are needed, include a table of concrete remaining tasks in this format:\n'+(MODEL_TABLE if run.get('model_routing') else TABLE)
    if task['role']=='integrator':
        return 'Please combine these checked results into a complete answer to my request:\n'+goal+reference+memory+screen_tools+'\nUse my language. State what was actually checked and any remaining limitations.'
    return 'My overall request (for context; other parts are handled separately):\n'+goal+memory+'\nYour assigned part (do only this part):\n'+task['prompt']+project+run.get('file_claims','')+tools+screen_tools+reference+_result_file_hint(task,run,tools_available and bool(tools))+'\nPlease reply with the result of your assigned part only, with this check: '+task['acceptance']+'. Do not answer or repeat the other parts of my request. If anything needed is unavailable, explain that clearly.'
