"""Notion-native response formats: normal project questions and readable task tables.

The coordinator owns all roles and state. It never asks Notion to impersonate a
system agent. Machine contracts are normalized outside the model.
"""
from html import unescape
from html.parser import HTMLParser
import re

from .protocol import ContractError,validate_plan

class ProviderReplyError(ContractError):
    pass

def provider_error(text):
    return bool(re.fullmatch(r'(?is)\s*(?:something went wrong|an error occurred|internal server error)[.!]?\s*(?:please try again(?: later)?[.!]?)?\s*',plain(text)))


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


def table_plan(text):
    rows=[]
    for line in plain(text).splitlines():
        cells=[c.strip().strip('`') for c in line.strip().strip('|').split('|')]
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


def normalize(task,text):
    if provider_error(text):raise ProviderReplyError('Notion returned a provider error instead of an answer')
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
        match=re.search(r'(?im)^\s*(?:#+\s*)?(PASS|REPAIR|All checks passed|Changes needed)\b',readable)
        if not match:raise ContractError('Review needs an explicit checked verdict')
        passed=match.group(1).lower() in ('pass','all checks passed')
        return dict(passed=passed,summary=readable,fixes=[] if passed else table_plan(text)['tasks'])
    if task['role']=='executor':
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
For files, use paths relative to the project folder separated by semicolons, or none.
Use one line per task; do not put vertical bars inside a cell.'''

MODEL_TABLE='''| ID | Title | Work to do | Depends on | Files | How to check | Model | Model reason | Route |
| T1 | short title | specific instructions | none | none | observable check | Opus 5.5 | explain need | worker |
Choose explicitly Haiku 5.5 for bounded routine work, Sonnet 5.5 for moderate coding/analysis,
and Opus 5.5 for difficult reasoning, ambiguity or high consequence work. Never choose Auto.
Prioritize answer quality and latency; cheap models are useful only when capable of the task.
Choose afresh for every task, including repairs; never inherit the previous worker model.
Route is worker for delegation. If you can solve the request efficiently alone, return exactly
one independent task with Route main and Model Opus 5.5. Include its complete checks.
Use T1, T2, etc.; list dependencies or none, and relative files separated by semicolons or none.
One line per task, no vertical bars inside a cell.'''


# Visual work (desktop apps, UI, renders) needs the screen tools even without a project folder.
VISUAL=re.compile(r'\b(roblox|blender|screens?|screenshots?|capture|viewport|gui|ui|layout|render(ing)?|visual(ly)?|bildschirm|fenster|aussehen)\b',re.I)
SCREEN_HINT=(' For visual work in desktop apps such as Roblox Studio or Blender, call the screen_capture tool through mcp_call'
             ' (no target = the whole screen; target "roblox" or "blender" = only that window) as often as needed to see the current state'
             ' before and after changes. Screen tools need no assignment token or code.')


def visual(task,run):
    text=' '.join(str(value or '') for value in (run.get('goal'),task.get('title'),task.get('prompt'),task.get('acceptance')))
    return bool(VISUAL.search(text))


def prompt(task,run,context,token,tools_available):
    goal=run['goal']
    reference=('\nFor reference, the work already completed:\n'+context+'\n') if context.strip() else ''
    memory=('\nFor reference, our completed earlier work:\n'+run['context']+'\n') if run.get('context','').strip() else ''
    project=('\nThe project folder is '+run['project_path']+'.\n') if run.get('project_path') else ''
    screen=bool(tools_available) and visual(task,run)
    tools=('\nThe connected NotionTool project tools require assignment_token '+token+' for this request.'+(SCREEN_HINT if screen else '')+'\n') if tools_available and (run.get('project_path') or task.get('files') or screen) else ''
    if run.get('allocation',{}).get('mode')=='direct':
        return goal+memory+'\nReply naturally in my language.'
    if task['role'] in ('architect','researcher','quality_planner'):
        angle={'architect':'a clear method and useful independent parts','researcher':'facts, calculations and practical constraints',
               'quality_planner':'specific checks and possible mistakes'}[task['role']]
        return 'I would like help planning this request:\n'+goal+'\nPlease suggest '+angle+'. I only need suggestions for now, not execution.'+memory+project
    if task['role']=='planner':
        correction='\n'+task['prompt'] if task.get('attempts',0)>1 else ''
        explicit=run.get('allocation',{}).get('requested_parts')
        independence=f'\nI requested exactly {explicit} independent execution contributions. Give exactly {explicit} rows with no dependencies; each part must compute and check its own result without relying on another row. Do not add a review or combine row.' if explicit else ''
        return 'You are the Architect of my request. Decide whether to work alone or delegate, and assign the explicit model of every task. Please make an execution task list:\n'+goal+memory+reference+project+f'\nUp to {run.get("worker_budget",run["capacity"])} people can contribute independent parts. Choose the smallest useful task list that preserves answer quality and speed. Do not invent work to fill available people. Separate independent work only when it saves time or improves quality; avoid duplicate research and merging overhead. Respect any explicit contribution count in my request. Use dependencies where needed. Delegated work will be reviewed and combined separately.'+independence+correction+'\nPlease present the task list as this readable table:\n'+(MODEL_TABLE if run.get('model_routing') else TABLE)
    if task['role']=='reviewer':
        return 'Please check these completed results against my request:\n'+goal+reference+project+tools+'\nExplain what you checked. Give the verdict on a separate line: All checks passed, or Changes needed. If changes are needed, include a table of concrete remaining tasks in this format:\n'+(MODEL_TABLE if run.get('model_routing') else TABLE)
    if task['role']=='integrator':
        return 'Please combine these checked results into a complete answer to my request:\n'+goal+reference+memory+'\nUse my language. State what was actually checked and any remaining limitations.'
    return 'My overall request (for context; other parts are handled separately):\n'+goal+memory+'\nYour assigned part (do only this part):\n'+task['prompt']+project+tools+reference+'\nPlease reply with the result of your assigned part only, with this check: '+task['acceptance']+'. Do not answer or repeat the other parts of my request. If anything needed is unavailable, explain that clearly.'
