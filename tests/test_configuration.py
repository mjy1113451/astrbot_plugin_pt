import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_patpat_schema_only_exposes_speed(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema), {"patpat", "platform_tokens"})
        patpat = schema["patpat"]
        self.assertEqual(patpat["type"], "object")
        self.assertEqual(set(patpat["items"]), {"speed"})
        self.assertEqual(
            patpat["items"]["speed"],
            {
                "description": "摸头速度",
                "type": "float",
                "default": 1.0,
                "hint": "控制摸头动作的快慢，值越大越快。建议范围：0.5 ~ 3.0",
                "slider": {"min": 0.5, "max": 3.0, "step": 0.1},
            },
        )

    def test_platform_tokens_schema_exposes_credentials(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        tokens = schema["platform_tokens"]
        self.assertEqual(tokens["type"], "object")
        self.assertEqual(
            set(tokens["items"]),
            {
                "telegram_bot_token",
                "discord_bot_token",
                "slack_bot_token",
                "feishu_app_id",
                "feishu_app_secret",
            },
        )
        for key in ("telegram_bot_token", "discord_bot_token", "slack_bot_token", "feishu_app_secret"):
            self.assertTrue(tokens["items"][key].get("secret"), key)
            self.assertEqual(tokens["items"][key]["default"], "", key)

    def test_static_config_remains_unchanged(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"trigger": "摸摸", "interval": 0.06})


if __name__ == "__main__":
    unittest.main()
