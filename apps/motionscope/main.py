#
# This file is part of Vizy 
#
# All Vizy source code is provided under the terms of the
# GNU General Public License v2 (http://www.gnu.org/licenses/gpl-2.0.html).
# Those wishing to use Vizy source code, software and/or
# technologies under different licensing terms should contact us at
# support@charmedlabs.com. 
#

import os
from threading import Thread, RLock
from kritter import Kritter, import_config
import kritter
import time
import json
import collections
from dash_devices.dependencies import Input, Output
import dash_bootstrap_components as dbc
import dash_html_components as html
from vizy import Vizy, Perspective
import vizy.vizypowerboard as vpb
from camera import Camera 
from capture import Capture
from process import Process
from analyze import Analyze
from tab import Tab

"""
todo:

testing:
xtest null case (no motion)
xtest short vid < BG_CNT_FINAL frames
transitions -- load file while processing, move to capture while processing (then back again)

documentation:
data:
0 pts
1 index
2 x centroid
3 y centroid
4 rect-x
5 rect-y
6 rect-width
7 rect-height

"""

CONSTS_FILE = "motionscope_consts.py"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
MEDIA_DIR = os.path.join(APP_DIR, "media")


def get_projects():
    projects = os.listdir(MEDIA_DIR)
    projects = [p.split(".")[0] for p in projects]
    projects = set(projects)
    projects = [p for p in projects if os.path.exists(os.path.join(MEDIA_DIR, f"{p}.data"))]
    projects.sort()
    return projects

class SaveAsDialog(kritter.Kdialog):
    def __init__(self):
        self.name = ''
        self.callback_func = None
        name = kritter.KtextBox(placeholder="Enter project name")
        save_button = kritter.Kbutton(name=[Kritter.icon("save"), "Save"], disabled=True)
        overwite_text = kritter.Ktext(style={"control_width": 12})
        yesno = kritter.KyesNoDialog(title="Overwrite project?", layout=overwite_text, shared=True)
        name.append(save_button)
        super().__init__(title="Save project as", close_button=[Kritter.icon("close"), "Cancel"], layout=[name, yesno], shared=True)

        @self.callback_view()
        def func(state):
            if not state:
                return name.out_value("")

        @name.callback()
        def func(val):
            if val:
                self.name = val
            return save_button.out_disabled(not bool(val))

        @save_button.callback()
        def func():
            projects = get_projects()
            if self.name in projects:
                return overwite_text.out_value(f'"{self.name}" exists. Do you want to overwrite?') + yesno.out_open(True)                
            self.kapp.push_mods(self.out_open(False))
            if self.callback_func:
                self.callback_func(self.name)

        @yesno.callback_response()
        def func(val):
            if val:
                self.kapp.push_mods(self.out_open(False))
                if self.callback_func:
                    self.callback_func(self.name)

    def callback_name(self):
        def wrap_func(func):
            self.callback_func = func
        return wrap_func


class OpenProjectDialog(kritter.Kdialog):
    def __init__(self):
        self.selection = ''
        self.callback_func = None
        open_button = kritter.Kbutton(name=[Kritter.icon("folder-open"), "Open"], disabled=True)
        delete_button = kritter.Kbutton(name=[Kritter.icon("trash"), "Delete"], disabled=True)
        delete_text = kritter.Ktext(style={"control_width": 12})
        yesno = kritter.KyesNoDialog(title="Delete project?", layout=delete_text, shared=True)
        select = kritter.Kdropdown(value=None, placeholder="Select project...")
        select.append(open_button)
        select.append(delete_button)
        super().__init__(title="Open project", layout=[select, yesno], shared=True)

        @self.callback_view()
        def func(state):
            if state:
                projects = get_projects()
                return select.out_options(projects)
            else:
                return select.out_value(None)

        @select.callback()
        def func(selection):
            self.selection = selection
            disabled = not bool(selection)
            return open_button.out_disabled(disabled) + delete_button.out_disabled(disabled)

        @open_button.callback()
        def func():
            if self.callback_func:
                self.callback_func(self.selection)
            return self.out_open(False)

        @delete_button.callback()
        def func():
            return delete_text.out_value(f'Are you sure you want to delete "{self.selection}" project?') + yesno.out_open(True)

        @yesno.callback_response()
        def func(val):
            if val:
                os.remove(os.path.join(MEDIA_DIR, f"{self.selection}.data"))
                os.remove(os.path.join(MEDIA_DIR, f"{self.selection}.raw"))
                projects = get_projects()
                return select.out_options(projects)


    def callback_project(self):
        def wrap_func(func):
            self.callback_func = func
        return wrap_func


# Do a nested dictionary update
def deep_update(d1, d2):
    if all((isinstance(d, dict) for d in (d1, d2))):
        for k, v in d2.items():
            d1[k] = deep_update(d1.get(k), v)
        return d1
    return d2

class MotionScope:

    def __init__(self, media_dir):
        self.media_dir = media_dir
        if not os.path.isdir(self.media_dir):
            os.system(f"mkdir -p {self.media_dir}")
        self.data = collections.defaultdict(dict)
        self.kapp = Vizy()
        consts_filename = os.path.join(APP_DIR, CONSTS_FILE) 
        self.config_consts = import_config(consts_filename, self.kapp.etcdir, ["WIDTH", "PADDING", "GRAPHS", "MAX_RECORDING_DURATION", "START_SHIFT", "MIN_RANGE", "PLAY_RATE", "UPDATE_RATE", "FOCAL_LENGTH", "BG_AVG_RATIO", "BG_CNT_FINAL", "EXT_BUTTON_CHANNEL", "DEFAULT_CAMERA_SETTINGS", "DEFAULT_CAPTURE_SETTINGS", "DEFAULT_PROCESS_SETTINGS", "DEFAULT_ANALYZE_SETTINGS"])     
        self.lock = RLock()
        self.vpb = vpb.VizyPowerBoard()

        # Create and start camera.
        self.camera = kritter.Camera(hflip=True, vflip=True)
        self.camera.mode = "768x432x10bpp"

        style = {"label_width": 3, "control_width": 6, "max_width": self.config_consts.WIDTH}
        # Set video width to dynamically scale with width of window or WIDTH, whichever
        # is less.  We subtract 2*PADDING because it's on both sides. 
        self.video = kritter.Kvideo(overlay=True, video_style={"width": f"min(calc(100vw - {2*self.config_consts.PADDING}px), {self.config_consts.WIDTH}px)"})
        self.perspective = Perspective(self.video, self.config_consts.FOCAL_LENGTH, self.camera.getmodes()[self.camera.mode], style=style)       
        self.camera_tab = Camera(self)
        self.capture_tab = Capture(self)
        self.process_tab = Process(self)
        self.analyze_tab = Analyze(self)
        self.tabs = [self.camera_tab, self.capture_tab, self.process_tab, self.analyze_tab]
        for t in self.tabs:
            t.id_nav = self.kapp.new_id()    
        self.tab = self.camera_tab

        self.file_options_map = {"open": dbc.DropdownMenuItem([Kritter.icon("folder-open"), "Open..."], disabled=True), "save": dbc.DropdownMenuItem([Kritter.icon("save"), "Save"], disabled=True), "save-as": dbc.DropdownMenuItem([Kritter.icon("save"), "Save as..."]), "close": dbc.DropdownMenuItem([Kritter.icon("folder"), "Close"], disabled=True)}
        self.file_menu = kritter.KdropdownMenu(name="File", options=list(self.file_options_map.values()), nav=True, item_style={"margin": "0px", "padding": "0px 10px 0px 10px"})
        self.sa_dialog = SaveAsDialog()
        self.open_dialog = OpenProjectDialog()

        nav_items = [dbc.NavItem(dbc.NavLink(t.name, active=i==0, id=t.id_nav, disabled=t.name=="Process" or t.name=="Analyze")) for i, t in enumerate(self.tabs)]
        nav_items.append(self.file_menu.control)
        nav = dbc.Nav(nav_items, pills=True, navbar=True)
        navbar = dbc.Navbar(nav, color="dark", dark=True, expand=True)

        self.save_progress_dialog = kritter.KprogressDialog(title="Saving...", shared=True)
        self.load_progress_dialog = kritter.KprogressDialog(title="Loading...", shared=True)

        controls_layout = html.Div([
            # Navbar stays fixed at top
            navbar, 
            # Everything else scrolls.
            html.Div([
                html.Div([
                    html.Div([self.video, 
                        dbc.Card([self.perspective.layout] + [t.layout for t in self.tabs], 
                            style={"max-width": f"{self.config_consts.WIDTH}px", "margin-top": "10px", "margin-bottom": "10px"}
                        )
                    ], style={"float": "left"}), 
                    html.Div(self.analyze_tab.graphs.layout)
                ], style={"padding": f"{self.config_consts.PADDING}px"})
            # Next Div is scrollable, occupies all of available viewport.    
            ], style={"overflow": "auto"})
        # Outermost Div is flexbox 
        ], style={"display": "flex", "height": "100%", "flex-direction": "column"})

        self.kapp.layout = [controls_layout, self.save_progress_dialog, self.load_progress_dialog, self.sa_dialog, self.open_dialog]

        @self.open_dialog.callback_project()
        def func(project):
            # Display load progress dialog to give user feedback.  
            self.kapp.push_mods(self.load_progress_dialog.out_progress(0) + self.load_progress_dialog.out_open(True))
            # Reset state of application to make sure no remnant settings are left behind.
            self.kapp.push_mods(self.reset())
            self.set_project(project)
            filename = os.path.join(self.media_dir, f"{self.data['project']}.raw")
            exists = os.path.exists(filename)
            self.run_progress = True
            # Create recording object (save_load_progress needs it)
            if exists:
                self.data['recording'] = self.camera.stream(False)
            Thread(target=self.save_load_progress, args=(self.load_progress_dialog, )).start()
            # Load (this blocks)
            if exists:
                self.data['recording'].load(filename)
            self.run_progress = False

        @self.sa_dialog.callback_name()
        def func(name):
            self.set_project(name)
            self.save()

        @self.file_menu.callback()
        def func(val):
            file_options = list(self.file_options_map.keys())
            ss = file_options[val]
            if ss=="open":
                return self.open_dialog.out_open(True)
            elif ss=="save":
                self.save()
                return
            elif ss=="save-as": 
                return self.sa_dialog.out_open(True)
            else: # ss=="close":
                return self.reset()

        for t in self.tabs:
            func = self.get_tab_func(t)
            self.kapp.callback_shared(None, [Input(t.id_nav, "n_clicks")])(func)
        
        @self.capture_tab.data_update_callback
        def func(changed, cmem):
            return self.data_update(changed, cmem)

        @self.process_tab.data_update_callback
        def func(changed, cmem):
            return self.data_update(changed, cmem)

        self.kapp.push_mods(self.load_update() + self.reset())

        # Run main gui thread.
        self.run_thread = True
        Thread(target=self.thread).start()

        # Run Kritter server, which blocks.
        self.kapp.run()
        self.run_thread = False

    def reset(self):
        mods = []
        # Reset tabs
        for t in self.tabs:
            mods += t.reset()
        # Push tab reset first to reset variables, etc. 
        self.kapp.push_mods(mods + self.perspective.out_reset() + self.perspective.out_enable(False))
        self.data['recording'] = None
        try:
            del self.file_options_map['header']
            del self.file_options_map['divider']
            del self.data['obj_data']
            del self.data['project']
        except KeyError:
            pass
        self.file_options_map['save'].disabled = True
        self.file_options_map['close'].disabled = True
        # Reset perspective and disable
        f = self.get_tab_func(self.camera_tab)
        return f(None) + [Output(self.analyze_tab.id_nav, "disabled", True), Output(self.process_tab.id_nav, "disabled", True)] + self.file_menu.out_options(list(self.file_options_map.values()))  

    def save(self):
        self.run_progress = True
        Thread(target=self.save_load_progress, args=(self.save_progress_dialog, )).start()
        if self.data['recording'] is not None:
            self.data['recording'].save(os.path.join(self.media_dir, f"{self.data['project']}.raw"))
        self.run_progress = False

    def set_project(self, project):
        self.data['project'] = project
        try:
            del self.file_options_map['header']
            del self.file_options_map['divider']
        except KeyError:
            pass
        self.file_options_map['save'].disabled = False
        self.file_options_map['close'].disabled = False
        self.file_options_map = {**{"header": dbc.DropdownMenuItem(self.data['project'], header=True), "divider": dbc.DropdownMenuItem(divider=True)}, **self.file_options_map}
        self.kapp.push_mods(self.file_menu.out_options(list(self.file_options_map.values())))

    def get_tab_func(self, tab):
        def func(val):
            mods = [Output(t.layout.id, "is_open", t is tab) for t in self.tabs] + [Output(t.id_nav, "active", t is tab) for t in self.tabs]
            with self.lock:
                res = self.tab.focus(False)
                if res:
                    mods += res
                self.tab = tab
                res = self.tab.focus(True)
            if res:
                mods += res
            return mods 
        return func

    def data_update(self, changed, cmem=None):
        mods = []
        for t in self.tabs:
            mods += t.data_update(changed, cmem)
        if "recording" in changed:
            if self.data['recording'].len()>self.config_consts.BG_CNT_FINAL: 
                mods += self.file_menu.out_options(list(self.file_options_map.values())) + [Output(self.process_tab.id_nav, "disabled", False)]
        if "obj_data" in changed:
            if self.data['obj_data']:
                f = self.get_tab_func(self.analyze_tab)
                mods += [Output(self.analyze_tab.id_nav, "disabled", False)] + f(None)
            else: 
                mods += [Output(self.analyze_tab.id_nav, "disabled", True)]

        return mods           

    def load_update(self):
        projects = get_projects() 
        self.file_options_map['open'].disabled = not bool(projects) 
        return self.file_menu.out_options(list(self.file_options_map.values()))

    def save_load_progress(self, dialog):
        self.kapp.push_mods(dialog.out_progress(0) + dialog.out_open(True)) 

        # Update progress while file is being saved/loaded.
        _progress = 0
        if self.data['recording'] is not None:
            while self.run_progress:
                progress = self.data['recording'].progress()
                if progress>_progress:
                    self.kapp.push_mods(dialog.out_progress(progress-2))
                    _progress = progress
                time.sleep(1/self.config_consts.UPDATE_RATE)
        self.kapp.push_mods(dialog.out_progress(99))

        mods = []
        # Save/load rest of data.
        filename = os.path.join(self.media_dir, f"{self.data['project']}.data")
        # Save
        if dialog is self.save_progress_dialog: 
            with open(filename, 'w') as f:
                self.data['Perspective'] = self.perspective.get_params()
                data = self.data.copy()
                # We don't need bg, and recording is already saved.
                if 'bg' in data:
                    del data['bg']
                if 'recording' in data:
                    del data['recording']
                json.dump(data, f, cls=kritter.JSONEncodeFromNumpy) 
            mods += self.load_update()
        # Load        
        else: 
            # Inform tabs that we have a recording.
            if self.data['recording'] is not None:
                mods += self.data_update("recording")
            try:
                with open(filename) as f:
                    data = json.load(f, cls=kritter.JSONDecodeToNumpy)
                deep_update(self.data, data)

                # Inform tabs that we have a list of changed
                changed = list(data.keys())
                mods += self.data_update(changed)
                # This will fire off draw events for graphs in a different thread...
                mods += self.perspective.set_params(self.data['Perspective'])
                # ...so let's make sure we draw graphs with updated perspective here to 
                # avoid the race condition.\
                mods += self.analyze_tab.graphs.out_draw()
            except Exception as e:
                print(f"Error loading: {e}")

        self.kapp.push_mods(mods)
        # Display for at least 1 second
        time.sleep(1)
        self.kapp.push_mods(dialog.out_open(False))

    def thread(self):

        while self.run_thread:
            time.sleep(1e-3) # A tiny sleep to reduce latency of other threads.
            with self.lock:
                # Get frame
                frame = self.tab.frame()
            # Send frame
            if isinstance(frame, tuple): 
                # Capture can send frameperiod with frame 
                # so it renders correctly
                frame_ = self.perspective.transform(frame[0])
                self.video.push_frame(frame_, frame[1])
            elif frame is not None:
                frame = self.perspective.transform(frame)
                self.video.push_frame(frame)
        self.vpb.led(0, 0, 0)



if __name__ == "__main__":
        ms = MotionScope(MEDIA_DIR)
