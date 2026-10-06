from django.conf import settings
from django.test.runner import DiscoverRunner


class TestRunner(DiscoverRunner):
    """Test runner del progetto: durante i test non si pubblica nulla sul broker MQTT vero."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.MQTT_ENABLED = False
