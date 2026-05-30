import time
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from bullpen.api import PluginRenderer
from bullpen.api.data import PluginData
from renderers.main import MainRenderer, timer_cond


class StubData(PluginData):
    def __init__(self, config=None):
        pass

    def update(self, force: bool = False):
        pass


class BasePlugin(PluginRenderer[StubData]):
    def __init__(self, config=None, layout=None, colors=None):
        pass

    def wait_time(self) -> float:
        return 0

    def render(self, data, canvas, graphics, scrolling_text_pos) -> Optional[int]:
        return None


class TestPluginRendererDefaults(unittest.TestCase):
    def setUp(self):
        self.plugin = BasePlugin()
        self.data = StubData()

    def test_is_active_returns_true_by_default(self):
        self.assertTrue(self.plugin.is_active(self.data))

    def test_can_render_returns_true_by_default(self):
        self.assertTrue(self.plugin.can_render(self.data))

    def test_is_active_can_be_overridden_to_false(self):
        class InactivePlugin(BasePlugin):
            def is_active(self, data):
                return False

        self.assertFalse(InactivePlugin().is_active(self.data))

    def test_can_render_can_be_overridden_to_false(self):
        class NoRenderPlugin(BasePlugin):
            def can_render(self, data):
                return False

        self.assertFalse(NoRenderPlugin().can_render(self.data))


class TestDrawPluginScreen(unittest.TestCase):
    def _make_renderer(self, plugin):
        matrix = MagicMock()
        canvas = MagicMock()
        canvas.width = 64
        matrix.CreateFrameCanvas.return_value = canvas
        matrix.SwapOnVSync.return_value = canvas

        data = MagicMock()
        data.config.rotation_screen_rules = {}
        data.network_issues = False
        data.plugin_data = {"test": StubData()}

        return MainRenderer(matrix, data, {"test": plugin})

    def test_render_loop_skipped_when_is_active_false(self):
        class InactivePlugin(BasePlugin):
            def is_active(self, data):
                return False

        plugin = InactivePlugin()
        plugin.render = MagicMock(return_value=None)
        r = self._make_renderer(plugin)

        with patch("driver.graphics", MagicMock(), create=True):
            r._MainRenderer__draw_plugin_screen("test", lambda: True)

        plugin.render.assert_not_called()

    def test_render_loop_skipped_when_can_render_false(self):
        class NoRenderPlugin(BasePlugin):
            def can_render(self, data):
                return False

        plugin = NoRenderPlugin()
        plugin.render = MagicMock(return_value=None)
        r = self._make_renderer(plugin)

        with patch("driver.graphics", MagicMock(), create=True):
            r._MainRenderer__draw_plugin_screen("test", lambda: True)

        plugin.render.assert_not_called()

    def test_render_loop_runs_when_both_active_and_can_render(self):
        call_count = 0

        class ActivePlugin(BasePlugin):
            def render(self, data, canvas, graphics, pos):
                nonlocal call_count
                call_count += 1
                return None

        plugin = ActivePlugin()
        r = self._make_renderer(plugin)

        cond = timer_cond(0.05)
        with patch("driver.graphics", MagicMock(), create=True):
            r._MainRenderer__draw_plugin_screen("test", cond)

        self.assertGreater(call_count, 0)

    def test_reset_called_after_loop(self):
        plugin = BasePlugin()
        plugin.reset = MagicMock()
        r = self._make_renderer(plugin)

        with patch("driver.graphics", MagicMock(), create=True):
            r._MainRenderer__draw_plugin_screen("test", lambda: False)

        plugin.reset.assert_called_once()


if __name__ == "__main__":
    unittest.main()
