import unittest
from unittest import mock

from notionhub.internal_api import InternalApiError
from toggl2notion import update_heatmap


class UpdateHeatmapTest(unittest.TestCase):
    @mock.patch.object(update_heatmap.time, "time", return_value=1234567890)
    @mock.patch.object(update_heatmap.NotionHubInternalClient, "from_env")
    @mock.patch.object(update_heatmap.NotionHubInternalClient, "is_available", return_value=True)
    def test_internal_url_refreshes_cache_once_before_creating_public_url(
        self, _is_available, from_env, _time
    ):
        client = from_env.return_value
        client.public_heatmap_url.return_value = (
            "https://i.notionhub.app/v1/heatmap/toggl?token=heat_test"
        )

        result = update_heatmap.build_heatmap_url()

        self.assertEqual(
            result,
            "https://i.notionhub.app/v1/heatmap/toggl?token=heat_test",
        )
        self.assertEqual(
            client.method_calls,
            [
                mock.call.get_heatmap({"type": "time", "refresh": "1"}),
                mock.call.public_heatmap_url(
                    {"type": "time", "format": "html", "v": "1234567890"}
                ),
            ],
        )

    @mock.patch.object(update_heatmap.time, "time", return_value=1234567890)
    @mock.patch.object(update_heatmap.NotionHubInternalClient, "from_env")
    @mock.patch.object(update_heatmap.NotionHubInternalClient, "is_available", return_value=True)
    @mock.patch.dict("os.environ", {"ACTIVATION_CODE": "legacy-code"}, clear=True)
    def test_unavailable_refresh_route_falls_back_to_legacy_url(
        self, _is_available, from_env, _time
    ):
        from_env.return_value.get_heatmap.side_effect = InternalApiError(
            "route unavailable", fallback_allowed=True
        )

        result = update_heatmap.build_heatmap_url()

        self.assertEqual(
            result,
            "https://togglapi.notionhub.app/toggl/heatmap"
            "?v=1234567890&activationCode=legacy-code",
        )
        from_env.return_value.public_heatmap_url.assert_not_called()

    @mock.patch.object(update_heatmap, "build_heatmap_url")
    @mock.patch.object(update_heatmap, "NotionHelper")
    def test_missing_heatmap_block_skips_refresh(self, notion_helper, build_url):
        notion_helper.return_value.heatmap_block_id = None

        update_heatmap.main()

        build_url.assert_not_called()
        notion_helper.return_value.update_heatmap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
