import unittest
from unittest.mock import MagicMock, call, patch

from checkin import Checker, CheckinResult, CheckinStatus, main


class DummyConfig:
    cookies_list = ["cookie-1"]
    DOMAINS = ["glados.cloud", "railgun.info"]
    EXCHANGE_PLANS = {"plan500": 500}
    exchange_plan = "plan500"
    verbose = False


class QuietTestCase(unittest.TestCase):
    def setUp(self):
        self.logger_patch = patch("checkin.logger")
        self.logger_patch.start()
        self.addCleanup(self.logger_patch.stop)


class DomainFallbackTests(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.checker = Checker(DummyConfig())

    def test_stops_after_first_domain_accepts_cookie(self):
        repeated = CheckinResult(
            1,
            "glados.cloud",
            status="重复签到",
            code=CheckinStatus.REPEAT,
        )

        with patch.object(self.checker, "_checkin_on_domain", return_value=repeated) as mocked:
            self.checker.checkin_all()

        self.assertEqual(1, mocked.call_count)
        self.assertEqual([repeated], self.checker.results)

    def test_falls_back_to_second_domain_and_keeps_one_result(self):
        unavailable = CheckinResult(1, "glados.cloud", status="当前域名不可用")
        success = CheckinResult(
            1,
            "railgun.info",
            status="签到成功",
            code=CheckinStatus.SUCCESS,
        )

        with patch.object(
            self.checker,
            "_checkin_on_domain",
            side_effect=[unavailable, success],
        ) as mocked:
            self.checker.checkin_all()

        self.assertEqual(
            [
                call("cookie-1", 1, "glados.cloud"),
                call("cookie-1", 1, "railgun.info"),
            ],
            mocked.call_args_list,
        )
        self.assertEqual([success], self.checker.results)

    def test_reports_one_failure_when_all_domains_reject_cookie(self):
        first_failure = CheckinResult(1, "glados.cloud", status="当前域名不可用")
        final_failure = CheckinResult(1, "railgun.info", status="当前域名不可用")

        with patch.object(
            self.checker,
            "_checkin_on_domain",
            side_effect=[first_failure, final_failure],
        ):
            self.checker.checkin_all()

        self.assertEqual([final_failure], self.checker.results)

    def test_skips_signin_calls_when_domain_is_unavailable(self):
        api = MagicMock()
        api.get_status.return_value = ("None 天", CheckinStatus.FAILURE.value)
        context = MagicMock()
        context.__enter__.return_value = api

        with patch("checkin.API", return_value=context):
            result = self.checker._checkin_on_domain("cookie-1", 1, "glados.cloud")

        self.assertEqual(CheckinStatus.FAILURE, result.code)
        self.assertEqual("当前域名不可用", result.status)
        api.checkin.assert_not_called()
        api.get_points.assert_not_called()
        api.exchange.assert_not_called()

    def test_skips_follow_up_calls_after_checkin_failure(self):
        api = MagicMock()
        api.get_status.return_value = ("10 天", CheckinStatus.SUCCESS.value)
        api.checkin.return_value = {
            "status": "签到失败",
            "code": CheckinStatus.FAILURE,
        }
        context = MagicMock()
        context.__enter__.return_value = api

        with patch("checkin.API", return_value=context):
            result = self.checker._checkin_on_domain("cookie-1", 1, "glados.cloud")

        self.assertEqual(CheckinStatus.FAILURE, result.code)
        api.get_points.assert_not_called()
        api.exchange.assert_not_called()


class ActionStatusTests(QuietTestCase):
    def test_repeat_is_not_a_failure(self):
        checker = Checker(DummyConfig())
        checker.results = [CheckinResult(1, "glados.cloud", code=CheckinStatus.REPEAT)]

        self.assertFalse(checker.has_failures())

    def test_real_failure_is_detected(self):
        checker = Checker(DummyConfig())
        checker.results = [
            CheckinResult(1, "glados.cloud", code=CheckinStatus.SUCCESS),
            CheckinResult(2, "railgun.info", code=CheckinStatus.FAILURE),
        ]

        self.assertTrue(checker.has_failures())

    def test_main_returns_success_and_sends_summary(self):
        config = MagicMock()
        config.cookies_list = ["cookie-1"]
        checker = MagicMock()
        checker.format_results.return_value = ("重复1", "内容", "日志")
        checker.has_failures.return_value = False
        push_service = MagicMock()

        with (
            patch("checkin.Config", return_value=config),
            patch("checkin.Checker", return_value=checker),
            patch("checkin.PushService", return_value=push_service),
        ):
            exit_code = main()

        self.assertEqual(0, exit_code)
        push_service.send.assert_called_once_with("重复1", "内容")

    def test_main_returns_failure_and_still_pushes(self):
        config = MagicMock()
        config.cookies_list = ["cookie-1"]
        checker = MagicMock()
        checker.format_results.return_value = ("失败1", "内容", "日志")
        checker.has_failures.return_value = True
        push_service = MagicMock()

        with (
            patch("checkin.Config", return_value=config),
            patch("checkin.Checker", return_value=checker),
            patch("checkin.PushService", return_value=push_service),
        ):
            exit_code = main()

        self.assertEqual(1, exit_code)
        push_service.send.assert_called_once_with("失败1", "内容")

    def test_missing_cookie_returns_failure(self):
        config = MagicMock()
        config.cookies_list = []
        push_service = MagicMock()

        with (
            patch("checkin.Config", return_value=config),
            patch("checkin.Checker") as checker_class,
            patch("checkin.PushService", return_value=push_service),
        ):
            exit_code = main()

        self.assertEqual(1, exit_code)
        checker_class.assert_not_called()
        push_service.send.assert_called_once()

    def test_config_error_returns_failure_without_second_exception(self):
        with (
            patch("checkin.Config", side_effect=ValueError("配置错误")),
            patch("checkin.PushService") as push_service_class,
        ):
            exit_code = main()

        self.assertEqual(1, exit_code)
        push_service_class.assert_not_called()


class ExchangeThresholdTests(QuietTestCase):
    def setUp(self):
        super().setUp()
        self.checker = Checker(DummyConfig())

    def run_with_points(self, points_text, points_number):
        api = MagicMock()
        api.get_status.return_value = ("10 天", CheckinStatus.SUCCESS.value)
        api.checkin.return_value = {
            "status": "签到成功",
            "points": "1",
            "code": CheckinStatus.SUCCESS,
        }
        api.get_points.return_value = (points_text, points_number)
        api.exchange.return_value = "兑换成功: plan500"
        context = MagicMock()
        context.__enter__.return_value = api

        with patch("checkin.API", return_value=context):
            result = self.checker._checkin_on_domain("cookie-1", 1, "glados.cloud")

        return result, api

    def test_skips_exchange_below_threshold(self):
        result, api = self.run_with_points("31 积分", 31)

        self.assertEqual("积分不足，跳过兑换 (31/500)", result.exchange)
        api.exchange.assert_not_called()

    def test_exchanges_at_threshold(self):
        result, api = self.run_with_points("500 积分", 500)

        self.assertEqual("兑换成功: plan500", result.exchange)
        api.exchange.assert_called_once_with("cookie-1", "plan500")

    def test_exchanges_above_threshold(self):
        _, api = self.run_with_points("501 积分", 501)

        api.exchange.assert_called_once_with("cookie-1", "plan500")

    def test_skips_exchange_when_points_query_fails(self):
        result, api = self.run_with_points("None 积分", None)

        self.assertEqual("积分查询失败，跳过兑换", result.exchange)
        api.exchange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
