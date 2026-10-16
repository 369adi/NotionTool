import sys, tempfile, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notiontool import screen_capture as sc


class Store:
    def __init__(self, d):
        self.directory = d

    def event(self, *a, **k):
        pass

    def settings(self):
        return {}


try:
    t = time.time()
    img, how = sc.grab_screen()
    print('grab_screen ok', img.size, img.mode, how, round((time.time() - t) * 1000), 'ms')
except Exception:
    traceback.print_exc()
try:
    with tempfile.TemporaryDirectory() as d:
        r = sc.ScreenCapture(Store(d)).call('screen_capture', {'max_side': 800})
        print('call ok', {k: v for k, v in r.items() if k != '_image'}, 'image bytes', len(str(r.get('_image', ''))))
except Exception:
    traceback.print_exc()
