# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Presentation-only icon selection and scalable artwork tests."""
import os
import unittest
from unittest.mock import Mock
from package_cleaner.gui_icons import ICONS, ICON_SIZES, icon_name

class IconFallbackTests(unittest.TestCase):
    def test_prefers_first_available_icon(self):
        for role,names in ICONS.items():
            theme=Mock();theme.has_icon.side_effect=lambda name:name in names
            self.assertEqual(icon_name(theme,role),names[0])
            theme.has_icon.side_effect=lambda name:name==names[-1]
            self.assertEqual(icon_name(theme,role),names[-1])
    def test_missing_theme_icons_leave_text_usable(self):
        theme=Mock();theme.has_icon.return_value=False
        for role in ICONS:self.assertIsNone(icon_name(theme,role))

@unittest.skipUnless(os.environ.get('PACKAGE_CLEANER_GTK_TESTS')=='1','Native GTK tests opt in')
class NativeIconsTests(unittest.TestCase):
    def test_render_sizes_and_theme_availability(self):
        import gi
        gi.require_version('Gtk','3.0')
        from gi.repository import Gtk
        from package_cleaner.gui_icons import application_pixbuf, themed_image, decorate_button
        for size in ICON_SIZES:
            pixbuf=application_pixbuf(size)
            self.assertEqual((pixbuf.get_width(),pixbuf.get_height()),(size,size))
            self.assertTrue(pixbuf.get_has_alpha())
        theme=Gtk.IconTheme.get_default()
        for role in ICONS:
            name=icon_name(theme,role)
            if name:
                self.assertTrue(theme.has_icon(name))
                self.assertEqual(themed_image(role).get_pixel_size(),16)
        button=Gtk.Button(label='Keep Permanently')
        decorate_button(button,'keep')
        self.assertEqual(button.get_label(),'Keep Permanently')
        self.assertTrue(button.get_always_show_image())


@unittest.skipUnless(os.environ.get('PACKAGE_CLEANER_GTK_TESTS') == '1', 'Native GTK tests opt in')
class ApplicationListIconTests(unittest.TestCase):
    def setUp(self):
        import gi
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk, GdkPixbuf
        import tempfile
        from pathlib import Path
        self.Gtk, self.Pixbuf = Gtk, GdkPixbuf
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def png(self, width, height):
        path = self.directory / f'{width}x{height}.png'
        pixbuf = self.Pixbuf.Pixbuf.new(self.Pixbuf.Colorspace.RGB, True, 8, width, height)
        pixbuf.fill(0xff000080)  # Semi-transparent pixels must survive resizing.
        pixbuf.savev(str(path), 'png', [], [])
        return str(path)

    def check_image(self, icon, expected_edge=48):
        from package_cleaner.gui_icons import application_list_image
        image = application_list_image(icon)
        window = self.Gtk.Window()
        window.add(image)
        window.show_all()
        self.addCleanup(window.destroy)
        while self.Gtk.events_pending():
            self.Gtk.main_iteration()
        self.assertEqual(image.get_pixel_size(), expected_edge)
        self.assertLessEqual(image.get_preferred_width().natural_width, 48)
        self.assertLessEqual(image.get_preferred_height().natural_height, 48)
        self.assertLessEqual(image.get_allocated_width(), 48)
        self.assertLessEqual(image.get_allocated_height(), 48)
        self.assertEqual(image.get_valign(), self.Gtk.Align.CENTER)
        return image

    def loaded_pixels(self, image):
        theme = self.Gtk.IconTheme.get_default()
        if image.get_storage_type() == self.Gtk.ImageType.GICON:
            info = theme.lookup_by_gicon_for_scale(image.get_gicon()[0], image.get_pixel_size(),
                                                   image.get_scale_factor(), self.Gtk.IconLookupFlags.FORCE_SIZE)
        else:
            info = theme.lookup_icon_for_scale(image.get_icon_name()[0], image.get_pixel_size(),
                                                image.get_scale_factor(), self.Gtk.IconLookupFlags.FORCE_SIZE)
        pixbuf = info.load_icon()
        self.assertLessEqual(pixbuf.get_width(), 48 * image.get_scale_factor())
        self.assertLessEqual(pixbuf.get_height(), 48 * image.get_scale_factor())
        return pixbuf

    def test_oversized_png_preserves_alpha(self):
        image = self.check_image(self.png(256, 256))
        pixbuf = self.loaded_pixels(image)
        self.assertEqual(pixbuf.get_width(), 48 * image.get_scale_factor())
        self.assertTrue(pixbuf.get_has_alpha())
        self.assertEqual(pixbuf.get_pixels()[3], 128)

    def test_oversized_svg(self):
        path = self.directory / 'large.svg'
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="green" fill-opacity=".5"/></svg>')
        pixbuf = self.loaded_pixels(self.check_image(str(path)))
        self.assertEqual(pixbuf.get_width(), pixbuf.get_height())
        self.assertTrue(pixbuf.get_has_alpha())
        self.assertIn(pixbuf.get_pixels()[3], (127, 128))

    def test_non_square_icons_keep_aspect_ratio(self):
        for width, height in ((256, 128), (128, 256)):
            with self.subTest(width=width, height=height):
                pixbuf = self.loaded_pixels(self.check_image(self.png(width, height)))
                self.assertEqual(pixbuf.get_width() * height, pixbuf.get_height() * width)

    def test_small_icon_is_not_enlarged_logically(self):
        image = self.check_image(self.png(24, 24), 24)
        pixbuf = self.loaded_pixels(image)
        self.assertLessEqual(pixbuf.get_width() / image.get_scale_factor(), 24)

    def test_standard_theme_icon(self):
        image = self.check_image('application-x-executable')
        self.assertEqual(image.get_icon_name()[0], 'application-x-executable')
        self.loaded_pixels(image)

    def test_theme_with_only_an_oversized_fixed_entry(self):
        import shutil
        theme = self.Gtk.IconTheme.get_default()
        old_paths = theme.get_search_path()
        self.addCleanup(theme.set_search_path, old_paths)
        base = self.directory / 'hicolor'
        icons = base / '128x128/apps'
        icons.mkdir(parents=True)
        (base / 'index.theme').write_text('[Icon Theme]\nName=Hicolor\nDirectories=128x128/apps\n[128x128/apps]\nSize=128\nType=Fixed\nContext=Applications\n')
        name = 'package-cleaner-test-fixed-128'
        shutil.copyfile(self.png(128, 128), icons / (name + '.png'))
        theme.prepend_search_path(str(self.directory))
        self.assertTrue(theme.has_icon(name))
        image = self.check_image(name)
        self.assertEqual(image.get_icon_name()[0], name)
        self.loaded_pixels(image)

    def test_missing_invalid_and_unreadable_icons_fall_back(self):
        from unittest.mock import patch
        from gi.repository import GLib
        invalid = self.directory / 'invalid.png'
        invalid.write_text('not an image')
        for path in (str(self.directory / 'missing.png'), str(invalid), 'nonexistent-package-cleaner-icon'):
            with self.subTest(path=path):
                self.assertEqual(self.check_image(path).get_icon_name()[0], 'application-x-executable')
        # Deterministic even when tests run as root, which can read mode-000 files.
        with patch.object(self.Pixbuf.Pixbuf, 'get_file_info', side_effect=GLib.Error('Permission denied')):
            self.assertEqual(self.check_image(self.png(256, 256)).get_icon_name()[0], 'application-x-executable')

    def test_theme_change_reloads_without_growing(self):
        image = self.check_image(self.png(256, 256))
        self.Gtk.IconTheme.get_default().emit('changed')
        self.assertEqual(image.get_pixel_size(), 48)
        self.loaded_pixels(image)
