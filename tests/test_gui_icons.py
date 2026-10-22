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
