#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""Render the real GTK frontend with fictional inventory, never host discovery."""
import argparse
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from package_cleaner.core import ReviewService, Preferences
from package_cleaner.core.inventory import Package
from package_cleaner.core.review_service import Discovery
from package_cleaner.core.recommendations import Recommendation, Evidence


def demo_service(directory):
    entries=[('Blender','3D creation suite',1400,'flatpak'),('LibreOffice','Office documents and spreadsheets',920,'flatpak'),
             ('Firefox','Web browser',310,'apt'),('GIMP','Image editor',280,'apt'),('VLC','Media player',170,'apt')]
    packages={};items=[]
    for title,description,mb,provider in entries:
        name='demo-'+title.lower()
        packages[name]=Package(name,name,'demo',False,mb*1024**2,description,'graphics',application=True,provider=provider)
        items.append(Recommendation(name,title,description,'',Evidence(),0,[],installed_bytes=mb*1024**2,provider=provider))
    class DemoService(ReviewService):
        def discover(self,*args,**kwargs):return Discovery(items,[])
    service=DemoService(Preferences(directory/'preferences'),directory/'queue',inventory_provider=lambda:(packages,[]))
    service.packages=packages
    service.selected={'demo-blender'}
    service.icons={n:'application-x-executable' for n in packages}
    return service


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('docs/images/review.png'))
    args=parser.parse_args()
    import gi
    gi.require_version('Gtk','3.0')
    from gi.repository import Gtk,GLib,Gdk
    from package_cleaner.gui import Window
    app=Gtk.Application(application_id='org.packagecleaner.Demonstration');app.register(None)
    settings=Gtk.Settings.get_default();settings.set_property('gtk-theme-name','Mint-Y')
    settings.set_property('gtk-application-prefer-dark-theme',False)
    with tempfile.TemporaryDirectory() as tmp,patch('package_cleaner.core.authorization.execute_confirmed',side_effect=AssertionError('Demo cannot execute')):
        window=Window(app,demo_service(Path(tmp)));window.resize(1100,800);window.present()
        deadline=time.monotonic()+10
        while window.busy and time.monotonic()<deadline:
            while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
            time.sleep(.01)
        assert not window.busy
        window.status.set_text('Demonstration data · Applications using at least 100 MB · 5 found')
        end=time.monotonic()+.5
        while time.monotonic()<end:
            while GLib.MainContext.default().pending():GLib.MainContext.default().iteration(False)
            time.sleep(.01)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        Gdk.pixbuf_get_from_window(window.get_window(),0,0,window.get_allocated_width(),window.get_allocated_height()).savev(str(args.output),'png',[],[])
        window.destroy()
if __name__=='__main__':main()
