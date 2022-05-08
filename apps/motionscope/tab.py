#
# This file is part of Vizy 
#
# All Vizy source code is provided under the terms of the
# GNU General Public License v2 (http://www.gnu.org/licenses/gpl-2.0.html).
# Those wishing to use Vizy source code, software and/or
# technologies under different licensing terms should contact us at
# support@charmedlabs.com. 
#

from dataupdate import DataUpdate

class Tab(DataUpdate):
    def __init__(self, name, kapp, data):
        super().__init__(data)
        self.name = name
        self.kapp = kapp
        self.focused = False

    def frame(self):
        return None

    def focus(self, state):
        self.focused = state
        return []

    def reset(self):
        return []