import unittest
import tempfile
import json
from unittest import mock
import pendulum

from toggl2notion import toggl
from toggl2notion.notion_helper import NotionHelper


class FakeQueryHelper(NotionHelper):
    def __init__(self, props):
        self.time_props = props
        self.time_data_source_id = "time-ds"
        self.captured_filter = None

    def query_all_by_filter(self, data_source_id, filter):
        self.captured_filter = filter
        return ["matched"]


class FakeBoundaryHelper(NotionHelper):
    def __init__(self):
        self.time_data_source_id = "time-ds"
        self.queries = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"results": [{"id": "page-1"}], "has_more": False}


class FakeDeletionHelper:
    def __init__(self):
        self.archived = []
        self.queried_range = None

    def query_toggl_entries_by_time_range(self, start_date, end_date):
        self.queried_range = (start_date, end_date)
        return [
            {"id": "keep-page", "properties": {"Id": {"number": 101}}},
            {"id": "delete-page", "properties": {"Id": {"number": 202}}},
        ]

    def get_toggl_id_from_time_page(self, page):
        return page.get("properties", {}).get("Id", {}).get("number")

    def archive_page(self, page_id):
        self.archived.append(page_id)
        return {"id": page_id, "archived": True}


class FakeDateIconHelper(NotionHelper):
    def __init__(self):
        self.day_data_source_id = "day-ds"
        self.week_data_source_id = "week-ds"
        self.month_data_source_id = "month-ds"
        self.year_data_source_id = "year-ds"
        self.calls = []

    def get_date_relation_id_by_range(
        self,
        name,
        data_source_id,
        icon,
        properties,
        start,
        end=None,
        title_prop="标题",
        cover=None,
    ):
        self.calls.append({
            "name": name,
            "data_source_id": data_source_id,
            "icon": icon,
        })
        return f"{data_source_id}:{name}"

    def get_title_property_name(self, _data_source_id, fallback="名称"):
        return fallback

    def find_date_page_by_range(self, _data_source_id, _start, _end):
        return None

    def get_date_icon_payload(self, date, kind, color="red", content=None):
        return {"kind": kind}


class FakePages:
    def __init__(self):
        self.create_kwargs = None
        self.update_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return {"id": "created"}

    def update(self, **kwargs):
        self.update_kwargs = kwargs
        return {"id": kwargs.get("page_id"), "in_trash": kwargs.get("in_trash")}


class FakeCreateHelper(NotionHelper):
    def __init__(self):
        self.client = type("Client", (), {"pages": FakePages()})()

    def normalize_parent(self, parent):
        return parent

    def resolve_media_payload(self, media):
        return media() if callable(media) else media


class FakeEnsureHelper:
    def ensure_time_id_property(self):
        return None


class FakeProcessEntryHelper:
    tag_data_source_id = "tag-ds"
    client_data_source_id = "client-ds"
    project_data_source_id = "project-ds"
    time_data_source_id = "time-ds"

    def __init__(self):
        self.relation_calls = []

    def get_relation_id(self, name, data_source_id, icon=None, properties=None, **kwargs):
        self.relation_calls.append(
            {
                "name": name,
                "data_source_id": data_source_id,
                "properties": dict(properties or {}),
            }
        )
        return f"{data_source_id}:{name}"

    def build_properties(self, _data_source_id, raw_properties, mandatory_properties=None):
        return raw_properties

    def get_date_relation(self, _properties, _date):
        return None


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code == 200
        self.text = str(payload)

    def json(self):
        return self._payload


class SyncLogicTest(unittest.TestCase):
    def test_workspace_cache_reports_partial_metadata_failure(self):
        responses = [
            FakeResponse([], 503),
            FakeResponse([], 200),
        ]
        with mock.patch.object(toggl.requests, "get", side_effect=responses):
            self.assertFalse(toggl.load_workspace_cache(123))

    def test_workspace_cache_reports_complete_metadata(self):
        responses = [
            FakeResponse([], 200),
            FakeResponse([], 200),
        ]
        with mock.patch.object(toggl.requests, "get", side_effect=responses):
            self.assertTrue(toggl.load_workspace_cache(123))

    def test_project_relation_does_not_require_optional_coin_property(self):
        helper = FakeProcessEntryHelper()
        original_helper = toggl.notion_helper
        original_projects = toggl.project_cache
        original_clients = toggl.client_cache
        toggl.notion_helper = helper
        toggl.project_cache = {
            101: {"name": "Project A", "client_id": 202, "workspace_id": 303}
        }
        toggl.client_cache = {202: "Client A"}
        try:
            toggl.process_entry(
                {
                    "id": 404,
                    "project_id": 101,
                    "description": "测试记录",
                    "start": "2026-07-22T08:00:00Z",
                    "stop": "2026-07-22T09:00:00Z",
                }
            )
        finally:
            toggl.notion_helper = original_helper
            toggl.project_cache = original_projects
            toggl.client_cache = original_clients

        project_call = next(
            call for call in helper.relation_calls if call["data_source_id"] == "project-ds"
        )
        self.assertNotIn("金币", project_call["properties"])
        self.assertIn("Client", project_call["properties"])

    def test_effective_sync_start_never_precedes_registration(self):
        registered = pendulum.datetime(2020, 5, 10, 12, tz="Asia/Shanghai")
        self.assertEqual(
            toggl.effective_sync_start(registered, {"syncStartDate": "2020-01-01"}),
            registered,
        )
        self.assertEqual(
            toggl.effective_sync_start(registered, {"syncStartDate": "2021-02-03"}).to_date_string(),
            "2021-02-03",
        )

    def test_get_created_at_uses_stored_profile_and_never_2010_fallback(self):
        original_auth = toggl.auth
        toggl.auth = object()
        try:
            with mock.patch.object(toggl.requests, "get", return_value=FakeResponse({}, 500)), mock.patch.dict(
                "os.environ",
                {"SERVICE_OPTIONS": json.dumps({"accountCreatedAt": "2022-03-04T10:00:00Z"})},
                clear=False,
            ):
                self.assertEqual(toggl.get_created_at().to_date_string(), "2022-03-04")
            with mock.patch.object(toggl.requests, "get", return_value=FakeResponse({}, 500)), mock.patch.dict(
                "os.environ", {"SERVICE_OPTIONS": "{}"}, clear=False
            ):
                with self.assertRaisesRegex(RuntimeError, "注册时间"):
                    toggl.get_created_at()
        finally:
            toggl.auth = original_auth

    def test_reverse_sync_option_defaults_on_and_can_be_disabled(self):
        with mock.patch.dict("os.environ", {"SERVICE_OPTIONS": "{}"}, clear=False):
            self.assertTrue(toggl.allow_reverse_sync())
        with mock.patch.dict("os.environ", {"SERVICE_OPTIONS": '{"allowReverseSync": false}'}, clear=False):
            self.assertFalse(toggl.allow_reverse_sync())

    def test_middle_gaps_are_clamped_to_configured_start(self):
        ranges = [
            (pendulum.datetime(2020, 1, 1, tz="Asia/Shanghai"), pendulum.datetime(2020, 1, 1, 1, tz="Asia/Shanghai")),
            (pendulum.datetime(2020, 3, 1, tz="Asia/Shanghai"), pendulum.datetime(2020, 3, 1, 1, tz="Asia/Shanghai")),
        ]
        original_helper = toggl.notion_helper
        toggl.notion_helper = type("Helper", (), {"query_time_entries_sorted_by_time": lambda self, toggl_only=True: [
            {"properties": {"时间": {"date": {"start": start.to_iso8601_string(), "end": end.to_iso8601_string()}}}}
            for start, end in ranges
        ]})()
        try:
            gaps = toggl.find_middle_gaps(lower_bound=pendulum.datetime(2020, 2, 1, tz="Asia/Shanghai"))
            self.assertEqual(gaps[0][0].to_date_string(), "2020-02-01")
        finally:
            toggl.notion_helper = original_helper
    def test_archive_page_uses_current_notion_trash_parameter(self):
        helper = FakeCreateHelper()

        result = helper.archive_page("page-to-trash")

        self.assertEqual(
            helper.client.pages.update_kwargs,
            {"page_id": "page-to-trash", "in_trash": True},
        )
        self.assertTrue(result["in_trash"])

    def test_trial_range_stops_before_fetching_when_quota_is_exhausted(self):
        class ExhaustedPolicy:
            is_trial = True

            def remaining(self, _stream):
                return 0

        original_helper = toggl.notion_helper
        original_policy = toggl.current_sync_policy
        original_get_entries = toggl.get_time_entries
        calls = {"entries": 0}
        toggl.notion_helper = FakeEnsureHelper()
        toggl.current_sync_policy = lambda: ExhaustedPolicy()

        def fake_get_entries(_start, _end):
            calls["entries"] += 1
            return [], 200

        toggl.get_time_entries = fake_get_entries
        try:
            result = toggl.sync_data_range(
                pendulum.now("Asia/Shanghai").subtract(days=2),
                pendulum.now("Asia/Shanghai"),
                ["workspace-1"],
            )
            self.assertTrue(result)
            self.assertEqual(calls["entries"], 0)
        finally:
            toggl.notion_helper = original_helper
            toggl.current_sync_policy = original_policy
            toggl.get_time_entries = original_get_entries

    def test_find_time_gaps_limits_large_middle_gaps(self):
        ranges = [
            (pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai"), pendulum.datetime(2026, 1, 1, 1, tz="Asia/Shanghai")),
            (pendulum.datetime(2026, 1, 12, tz="Asia/Shanghai"), pendulum.datetime(2026, 1, 12, 1, tz="Asia/Shanghai")),
            (pendulum.datetime(2026, 1, 25, tz="Asia/Shanghai"), pendulum.datetime(2026, 1, 25, 1, tz="Asia/Shanghai")),
        ]

        gaps = toggl.find_time_gaps(ranges, threshold_days=7, max_gaps=1)

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][0].to_datetime_string(), "2026-01-01 01:00:01")
        self.assertEqual(gaps[0][1].to_datetime_string(), "2026-01-11 23:59:59")

    def test_sync_stats_records_failures(self):
        stats = toggl.SyncStats()
        stats.add_success(was_update=False)
        stats.add_success(was_update=True)
        stats.add_failure("entry", "123", "boom")

        self.assertEqual(stats.summary(), "新增 1，更新 1，失败 1")
        self.assertIn("时间记录 123: boom", stats.failure_summary())

    def test_reverse_sync_requires_explicit_checkbox(self):
        helper = FakeQueryHelper({"Id": "number", "同步到 Toggl": "checkbox"})

        result = helper.query_entries_marked_for_toggl_sync()

        self.assertEqual(result, ["matched"])
        self.assertEqual(helper.captured_filter, {
            "and": [
                {"property": "Id", "number": {"is_empty": True}},
                {"property": "同步到 Toggl", "checkbox": {"equals": True}},
            ]
        })

    def test_reverse_sync_skips_when_checkbox_missing(self):
        helper = FakeQueryHelper({"Id": "number"})

        self.assertEqual(helper.query_entries_marked_for_toggl_sync(), [])
        self.assertIsNone(helper.captured_filter)

    def test_boundary_query_filters_to_toggl_linked_records(self):
        helper = FakeBoundaryHelper()

        page = helper.query_time_entry_boundary(direction="descending", toggl_only=True)

        self.assertEqual(page["id"], "page-1")
        self.assertEqual(helper.queries[0]["filter"], {"property": "Id", "number": {"is_not_empty": True}})
        self.assertEqual(helper.queries[0]["sorts"], [{"property": "时间", "direction": "descending"}])

    def test_gap_scan_filters_to_toggl_linked_records(self):
        helper = FakeBoundaryHelper()

        helper.query_time_entries_sorted_by_time(toggl_only=True)

        self.assertEqual(helper.queries[0]["filter"], {"property": "Id", "number": {"is_not_empty": True}})
        self.assertEqual(helper.queries[0]["sorts"], [{"property": "时间", "direction": "ascending"}])

    def test_sync_state_marks_checked_empty_gaps(self):
        start = pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai")
        end = pendulum.datetime(2026, 1, 10, tz="Asia/Shanghai")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/state.json"
            state = toggl.SyncState(path=path)

            self.assertFalse(state.is_empty_gap_checked(start, end))
            state.mark_empty_gap_checked(start, end)

            reloaded = toggl.SyncState(path=path)
            self.assertTrue(reloaded.is_empty_gap_checked(start, end))

    def test_find_middle_gaps_skips_checked_empty_gaps_before_limit(self):
        class FakeGapHelper:
            def query_time_entries_sorted_by_time(self, toggl_only=True):
                self.toggl_only = toggl_only
                return [
                    {"properties": {"时间": {"date": {"start": "2026-01-01T00:00:00+08:00", "end": "2026-01-01T01:00:00+08:00"}}}},
                    {"properties": {"时间": {"date": {"start": "2026-01-12T00:00:00+08:00", "end": "2026-01-12T01:00:00+08:00"}}}},
                    {"properties": {"时间": {"date": {"start": "2026-01-25T00:00:00+08:00", "end": "2026-01-25T01:00:00+08:00"}}}},
                ]

        original_helper = toggl.notion_helper
        helper = FakeGapHelper()
        toggl.notion_helper = helper
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                state = toggl.SyncState(path=f"{tmpdir}/state.json")
                state.mark_empty_gap_checked(
                    pendulum.datetime(2026, 1, 1, 1, 0, 1, tz="Asia/Shanghai"),
                    pendulum.datetime(2026, 1, 11, 23, 59, 59, tz="Asia/Shanghai"),
                )

                gaps = toggl.find_middle_gaps(max_gaps=1, state=state)

            self.assertTrue(helper.toggl_only)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0][0].to_datetime_string(), "2026-01-12 01:00:01")
        finally:
            toggl.notion_helper = original_helper

    def test_deletion_sync_archives_notion_pages_missing_from_toggl(self):
        original_helper = toggl.notion_helper
        helper = FakeDeletionHelper()
        toggl.notion_helper = helper
        try:
            stats = toggl.SyncStats()
            start = pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai")
            end = pendulum.datetime(2026, 1, 2, tz="Asia/Shanghai")

            toggl.sync_deleted_notion_entries(start, end, [{"id": 101}], stats)

            self.assertEqual(helper.archived, ["delete-page"])
            self.assertEqual(stats.archived, 1)
            self.assertIn("归档 1", stats.summary())
        finally:
            toggl.notion_helper = original_helper

    def test_deletion_sync_treats_server_deleted_entries_as_missing(self):
        original_helper = toggl.notion_helper
        helper = FakeDeletionHelper()
        toggl.notion_helper = helper
        try:
            stats = toggl.SyncStats()
            start = pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai")
            end = pendulum.datetime(2026, 1, 2, tz="Asia/Shanghai")

            toggl.sync_deleted_notion_entries(
                start,
                end,
                [{"id": 101}, {"id": 202, "server_deleted_at": "2026-01-02T00:00:00Z"}],
                stats,
            )

            self.assertEqual(helper.archived, ["delete-page"])
            self.assertEqual(stats.archived, 1)
        finally:
            toggl.notion_helper = original_helper

    def test_reports_api_sets_page_size_and_paginates_all_pages(self):
        original_get = toggl.requests.get
        original_sleep = toggl.time.sleep
        calls = []

        def fake_get(url, params=None, auth=None, headers=None, timeout=None):
            calls.append(dict(params))
            if params["page"] == 1:
                return FakeResponse({
                    "data": [{"id": index, "dur": 60000} for index in range(toggl.REPORTS_PAGE_SIZE)],
                    "per_page": toggl.REPORTS_PAGE_SIZE,
                    "total_count": toggl.REPORTS_PAGE_SIZE + 1,
                })
            return FakeResponse({
                "data": [{"id": toggl.REPORTS_PAGE_SIZE + 1, "dur": 60000}],
                "per_page": toggl.REPORTS_PAGE_SIZE,
                "total_count": toggl.REPORTS_PAGE_SIZE + 1,
            })

        toggl.requests.get = fake_get
        toggl.time.sleep = lambda _seconds: None
        try:
            entries, status_code = toggl.get_detailed_report(
                "workspace-1",
                pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai"),
                pendulum.datetime(2026, 1, 10, tz="Asia/Shanghai"),
            )

            self.assertEqual(status_code, 200)
            self.assertEqual(len(entries), toggl.REPORTS_PAGE_SIZE + 1)
            self.assertEqual(calls[0]["page_size"], toggl.REPORTS_PAGE_SIZE)
            self.assertEqual([call["page"] for call in calls], [1, 2])
        finally:
            toggl.requests.get = original_get
            toggl.time.sleep = original_sleep

    def test_reports_api_keeps_partial_page_at_subscription_boundary(self):
        original_get = toggl.requests.get
        original_sleep = toggl.time.sleep
        responses = [
            FakeResponse({
                "data": [{"id": index, "dur": 60000} for index in range(toggl.REPORTS_PAGE_SIZE)],
                "per_page": toggl.REPORTS_PAGE_SIZE,
            }),
            FakeResponse({"error": {"message": "Payment Required"}}, 402),
        ]
        toggl.requests.get = lambda *args, **kwargs: responses.pop(0)
        toggl.time.sleep = lambda _seconds: None
        try:
            entries, status_code = toggl.get_detailed_report(
                "workspace-1",
                pendulum.datetime(2026, 1, 1, tz="Asia/Shanghai"),
                pendulum.datetime(2026, 1, 10, tz="Asia/Shanghai"),
            )

            self.assertEqual(status_code, 402)
            self.assertEqual(len(entries), toggl.REPORTS_PAGE_SIZE)
        finally:
            toggl.requests.get = original_get
            toggl.time.sleep = original_sleep

    def test_reports_subscription_boundary_after_a_complete_range_is_success(self):
        original_helper = toggl.notion_helper
        original_get_historical_entries = toggl.get_historical_entries
        toggl.notion_helper = FakeEnsureHelper()
        responses = [([], 200), (None, 402)]
        toggl.get_historical_entries = lambda *_args: responses.pop(0)
        stats = toggl.SyncStats()
        try:
            end = pendulum.datetime(2026, 7, 21, tz="Asia/Shanghai")
            success = toggl.sync_data_range(
                end.subtract(days=20),
                end,
                ["workspace-1"],
                force_reports_api=True,
                stats=stats,
            )

            self.assertTrue(success)
            self.assertEqual(stats.failed, 0)
            self.assertEqual(len(stats.history_boundaries), 1)
            self.assertIn("历史边界 1", stats.summary())
        finally:
            toggl.notion_helper = original_helper
            toggl.get_historical_entries = original_get_historical_entries

    def test_reports_402_on_the_first_range_remains_a_failure(self):
        original_helper = toggl.notion_helper
        original_get_historical_entries = toggl.get_historical_entries
        toggl.notion_helper = FakeEnsureHelper()
        toggl.get_historical_entries = lambda *_args: (None, 402)
        stats = toggl.SyncStats()
        try:
            end = pendulum.datetime(2026, 7, 21, tz="Asia/Shanghai")
            success = toggl.sync_data_range(
                end.subtract(days=1),
                end,
                ["workspace-1"],
                force_reports_api=True,
                stats=stats,
            )

            self.assertFalse(success)
            self.assertEqual(stats.failed, 1)
            self.assertEqual(stats.history_boundaries, [])
        finally:
            toggl.notion_helper = original_helper
            toggl.get_historical_entries = original_get_historical_entries

    def test_known_newer_history_allows_an_immediate_subscription_boundary(self):
        original_helper = toggl.notion_helper
        original_get_historical_entries = toggl.get_historical_entries
        toggl.notion_helper = FakeEnsureHelper()
        toggl.get_historical_entries = lambda *_args: (None, 402)
        stats = toggl.SyncStats()
        try:
            end = pendulum.datetime(2026, 7, 21, tz="Asia/Shanghai")
            success = toggl.sync_data_range(
                end.subtract(days=1),
                end,
                ["workspace-1"],
                force_reports_api=True,
                stats=stats,
                newer_history_confirmed=True,
            )

            self.assertTrue(success)
            self.assertEqual(stats.failed, 0)
            self.assertEqual(len(stats.history_boundaries), 1)
        finally:
            toggl.notion_helper = original_helper
            toggl.get_historical_entries = original_get_historical_entries

    def test_standard_api_limit_guard_falls_back_to_reports_api(self):
        original_helper = toggl.notion_helper
        original_get_time_entries = toggl.get_time_entries
        original_get_historical_entries = toggl.get_historical_entries
        toggl.notion_helper = FakeEnsureHelper()
        calls = {"reports": 0}

        def fake_get_time_entries(_start, _end):
            return ([{"id": index, "start": "2026-07-01T00:00:00+08:00"} for index in range(toggl.TIME_ENTRIES_LIMIT_GUARD)], 200)

        def fake_get_historical_entries(_workspace_ids, _start, _end):
            calls["reports"] += 1
            return ([], 200)

        toggl.get_time_entries = fake_get_time_entries
        toggl.get_historical_entries = fake_get_historical_entries
        try:
            success = toggl.sync_data_range(
                pendulum.now("Asia/Shanghai").subtract(days=2),
                pendulum.now("Asia/Shanghai").subtract(days=1),
                ["workspace-1"],
            )

            self.assertTrue(success)
            self.assertEqual(calls["reports"], 1)
        finally:
            toggl.notion_helper = original_helper
            toggl.get_time_entries = original_get_time_entries
            toggl.get_historical_entries = original_get_historical_entries

    def test_date_relations_use_dynamic_icons(self):
        helper = FakeDateIconHelper()
        date = pendulum.datetime(2026, 7, 2, 13, 14, tz="Asia/Shanghai")

        helper.get_day_relation_id(date)
        helper.get_week_relation_id(date)
        helper.get_month_relation_id(date)
        helper.get_year_relation_id(date)
        calls_by_ds = {call["data_source_id"]: call for call in helper.calls}

        self.assertEqual(calls_by_ds["day-ds"]["icon"](), {"kind": "day"})
        self.assertEqual(calls_by_ds["week-ds"]["icon"](), {"kind": "week"})
        self.assertEqual(calls_by_ds["month-ds"]["icon"](), {"kind": "month"})
        self.assertEqual(calls_by_ds["year-ds"]["icon"](), {"kind": "year"})

    def test_day_relation_does_not_cascade_to_parent_dates(self):
        helper = FakeDateIconHelper()
        date = pendulum.datetime(2026, 7, 2, 13, 14, tz="Asia/Shanghai")

        helper.get_day_relation_id(date)

        self.assertEqual(len(helper.calls), 1)
        self.assertEqual(helper.calls[0]["data_source_id"], "day-ds")

    def test_create_page_resolves_lazy_icon_payload(self):
        helper = FakeCreateHelper()

        helper.create_page(
            {"data_source_id": "day-ds", "type": "data_source_id"},
            {"标题": "2026年07月02日"},
            icon=lambda: {"type": "emoji", "emoji": "📅"},
        )

        self.assertEqual(
            helper.client.pages.create_kwargs["icon"],
            {"type": "emoji", "emoji": "📅"},
        )


if __name__ == "__main__":
    unittest.main()
