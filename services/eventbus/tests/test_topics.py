"""Topic pattern matching rules."""

from atlas_eventbus.models import topic_matches


class TestTopicMatching:
    def test_exact_match(self):
        assert topic_matches("registry.service.registered", "registry.service.registered")

    def test_exact_mismatch(self):
        assert not topic_matches("registry.service.registered", "registry.service.removed")

    def test_prefix_wildcard(self):
        assert topic_matches("registry.*", "registry.service.registered")
        assert topic_matches("registry.*", "registry.anything")

    def test_prefix_wildcard_requires_prefix(self):
        assert not topic_matches("registry.*", "devices.zwave.on")

    def test_prefix_wildcard_does_not_match_bare_prefix(self):
        assert not topic_matches("registry.*", "registry")

    def test_wildcard_does_not_match_similar_prefix(self):
        # "registry.*" must not match "registryx.thing"
        assert not topic_matches("registry.*", "registryx.thing")

    def test_star_matches_everything(self):
        assert topic_matches("*", "anything.at.all")
