import pytest

from apps.support import devices


@pytest.mark.parametrize(
    ("agent", "browser", "system", "kind"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Chrome 131",
            "Windows",
            "desktop",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "Edge 131",
            "Windows",
            "desktop",
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Firefox 133",
            "macOS",
            "desktop",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
            "Safari 18",
            "iOS",
            "mobile",
        ),
        (
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Safari 17",
            "iPadOS",
            "tablet",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
            "Samsung Internet 25",
            "Android",
            "mobile",
        ),
        ("", "", "", ""),
    ],
)
def test_the_console_reads_the_browser_system_and_device(agent, browser, system, kind):
    assert devices.describe(agent) == devices.Device(browser, system, kind)
