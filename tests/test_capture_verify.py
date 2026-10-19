import builtins
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from notiontool import capture_verify
from notiontool.capture_verify import CHANGED_DISTANCE, CHANGED_RATIO, dhash, verify


def screen(sidebar=True, colours_swapped=False):
    image = Image.new('RGB', (200, 150), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 199, 24), fill=(40, 90, 200))
    if sidebar:
        draw.rectangle((0, 25, 49, 149), fill=(60, 60, 70))
    draw.rectangle((120, 100, 179, 129), fill=(40, 170, 90))
    for top in (40, 55, 70):
        draw.rectangle((65, top, 180, top + 5), fill=(30, 30, 30))
    if colours_swapped:
        red, green, blue = image.split()
        image = Image.merge('RGB', (blue, green, red))
    return image


class CaptureVerifyTests(unittest.TestCase):
    def assertCrossesLimit(self, result):
        self.assertTrue(result['changed'])
        self.assertGreaterEqual(result['diff_ratio'], CHANGED_RATIO)

    def test_identical_images(self):
        result = verify(screen(), screen())
        self.assertEqual(set(result), {'changed', 'distance', 'diff_ratio', 'summary'})
        self.assertEqual(result['distance'], 0)
        self.assertEqual(result['diff_ratio'], 0)
        self.assertFalse(result['changed'])
        self.assertTrue(result['summary'].startswith('No visible change'))

    def test_dhash_is_64_bit(self):
        value = dhash(screen())
        self.assertIsInstance(value, int)
        self.assertLess(value, 2 ** 64)
        self.assertEqual(value, dhash(screen()))

    def test_noise_stays_below_limits(self):
        after = screen()
        after.putpixel((150, 140), (245, 245, 245))
        result = verify(screen(), after)
        self.assertEqual(result['diff_ratio'], 0)
        ImageDraw.Draw(after).rectangle((150, 140, 151, 141), fill=(0, 0, 0))
        result = verify(screen(), after)
        self.assertGreater(result['diff_ratio'], 0)
        self.assertLess(result['diff_ratio'], CHANGED_RATIO)
        self.assertLess(result['distance'], CHANGED_DISTANCE)
        self.assertFalse(result['changed'])

    def test_small_change_is_detected(self):
        after = screen()
        ImageDraw.Draw(after).rectangle((170, 5, 189, 24), fill=(220, 40, 40))
        result = verify(screen(), after)
        self.assertCrossesLimit(result)
        self.assertLess(result['diff_ratio'], 0.05)

    def test_large_change_is_detected(self):
        result = verify(screen(), Image.new('RGB', (200, 150), (10, 10, 10)))
        self.assertCrossesLimit(result)
        self.assertGreater(result['diff_ratio'], 0.5)
        self.assertTrue(result['summary'].startswith('Changed'))

    def test_broken_screen_missing_panel(self):
        result = verify(screen(), screen(sidebar=False))
        self.assertCrossesLimit(result)
        self.assertGreater(result['diff_ratio'], 0.15)

    def test_broken_screen_swapped_colours(self):
        result = verify(screen(), screen(colours_swapped=True))
        self.assertCrossesLimit(result)
        self.assertGreater(result['diff_ratio'], 0.15)

    def test_same_content_at_other_size(self):
        before = screen()
        result = verify(before, before.resize((400, 300), Image.Resampling.NEAREST))
        self.assertLess(result['distance'], CHANGED_DISTANCE)
        self.assertLess(result['diff_ratio'], CHANGED_RATIO)
        self.assertFalse(result['changed'])
        self.assertIn('200x150 -> 400x300', result['summary'])

    def test_broken_screen_at_other_size(self):
        after = screen(sidebar=False).resize((400, 300), Image.Resampling.NEAREST)
        self.assertCrossesLimit(verify(screen(), after))

    def test_rgba_p_and_l_modes(self):
        base = screen()
        for other in (base.convert('RGBA'), base.copy()):
            result = verify(base, other)
            self.assertEqual((result['distance'], result['diff_ratio']), (0, 0))
        palette = base.convert('P', palette=Image.Palette.ADAPTIVE, colors=16)
        gray = base.convert('L')
        for image in (palette, gray):
            result = verify(image, image.convert('RGB'))
            self.assertEqual((result['distance'], result['diff_ratio']), (0, 0))
            self.assertFalse(result['changed'])
        transparent = Image.new('RGBA', (200, 150), (0, 0, 0, 0))
        result = verify(transparent, Image.new('RGB', (200, 150), (255, 255, 255)))
        self.assertEqual((result['distance'], result['diff_ratio']), (0, 0))
        self.assertCrossesLimit(verify(base, gray))

    def test_bad_input(self):
        for value in (None, b'png', 'before.png', 42):
            with self.assertRaises(TypeError):
                verify(value, screen())
            with self.assertRaises(TypeError):
                verify(screen(), value)
        with self.assertRaises(ValueError):
            verify(Image.new('RGB', (0, 0)), screen())

    def test_no_disk_writes(self):
        source = Path(capture_verify.__file__).read_text(encoding='utf-8')
        self.assertNotIn('save(', source)
        self.assertNotIn('open(', source)
        before, after = screen(), screen(sidebar=False)

        def refuse(*args, **kwargs):
            raise AssertionError('disk access attempted')

        with mock.patch.object(builtins, 'open', refuse), mock.patch('io.open', refuse), \
                mock.patch('os.open', refuse), mock.patch.object(Image.Image, 'save', refuse):
            result = verify(before, after)
        self.assertTrue(result['changed'])


if __name__ == '__main__':
    unittest.main()
