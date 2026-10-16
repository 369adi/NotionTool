"""Wire screen capture into bridge.py (exact-count edits, backup, dry run by default)."""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITS = {
    'notiontool/bridge.py': [
        ("READ_ONLY_ROLES = ('architect','researcher','quality_planner','planner','reviewer')\n",
         "READ_ONLY_ROLES = ('architect','researcher','quality_planner','planner','reviewer')\n"
         "from .screen_capture import SCREEN_TOOLS, SCREEN_OPERATOR_TOOLS, SCREEN_OPERATOR_NAMES, ScreenCapture\n"),
        ("OPERATOR_TOOLS = OPERATOR_TOOLS + DEV_TOOLS\n",
         "OPERATOR_TOOLS = OPERATOR_TOOLS + DEV_TOOLS + SCREEN_OPERATOR_TOOLS\n"),
        ("    def browser_access(self):\n        return bool(self.store.settings().get('mcp_browser_access', False))\n",
         "    def browser_access(self):\n        return bool(self.store.settings().get('mcp_browser_access', False))\n\n"
         "    def screen_access(self):\n"
         "        # Window capture is read-only and on by default; Settings can switch it off.\n"
         "        return bool(self.store.settings().get('screen_capture', True))\n\n"
         "    def screen(self):\n"
         "        if getattr(self, '_screen', None) is None:\n"
         "            self._screen = ScreenCapture(self.store)\n"
         "        return self._screen\n"),
        ("        if self.browser_access():\n            tools.update(BROWSER_TOOLS)\n",
         "        if self.browser_access():\n            tools.update(BROWSER_TOOLS)\n"
         "        if self.screen_access():\n            tools.update(SCREEN_TOOLS)\n"),
        ("            if name in TOOL_SERVER:\n                pass\n            elif name in BROWSER_TOOLS:\n",
         "            if name in TOOL_SERVER:\n                pass\n            elif name in SCREEN_TOOLS:\n                pass\n"
         "            elif name in BROWSER_TOOLS:\n"),
        ("        if name in BROWSER_TOOLS:\n            # Slow browser work runs outside the coordinator lock.\n",
         "        if name in SCREEN_TOOLS:\n"
         "            # Window capture is read-only and runs outside the coordinator lock.\n"
         "            return self.screen().call(name, args, task_id=task['id'], run_id=run['id'], role=task.get('role', ''))\n"
         "        if name in BROWSER_TOOLS:\n            # Slow browser work runs outside the coordinator lock.\n"),
        ("        if name in DEV_NAMES: return dev_call(name, args)\n",
         "        if name in SCREEN_OPERATOR_NAMES: return self.screen().call(name[3:], args, role='operator')\n"
         "        if name in DEV_NAMES: return dev_call(name, args)\n"),
        ("                        value.append({'id':'Browser','tools':list(BROWSER_TOOLS)})\n",
         "                        value.append({'id':'Browser','tools':list(BROWSER_TOOLS)})\n"
         "                    if self.screen_access():\n"
         "                        value.append({'id':'Screen','tools':list(SCREEN_TOOLS)})\n"),
        ("                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}\n",
         "                image = value.pop('_image', None) if isinstance(value, dict) else None\n"
         "                result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}\n"
         "                if image:\n"
         "                    result['content'].append({'type':'image','data':image['data'],'mimeType':image['mimeType']})\n"),
    ],
}


def main(apply):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    plans, problems = [], []
    for rel, edits in EDITS.items():
        path = ROOT / rel
        with open(path, encoding='utf-8', newline='') as f:
            text = f.read()
        crlf = '\r\n' in text
        new = text
        for index, (old, repl) in enumerate(edits, 1):
            if crlf:
                old, repl = old.replace('\n', '\r\n'), repl.replace('\n', '\r\n')
            found = new.count(old)
            if found != 1:
                problems.append(f'{rel} edit {index}: found {found} times')
                continue
            new = new.replace(old, repl)
        plans.append((rel, path, text, new, crlf))
    for problem in problems:
        print('PROBLEM', problem)
    if problems:
        print('nothing changed')
        return 1
    for rel, path, text, new, crlf in plans:
        print(f'{rel}: {len(EDITS[rel])} edits ok, crlf={crlf}, {len(text)} -> {len(new)} chars')
        if apply:
            backup = ROOT / 'work' / 'backups' / f'screen-{stamp}' / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(new)
            print('applied, backup', backup)
    return 0


if __name__ == '__main__':
    sys.exit(main('--apply' in sys.argv))
