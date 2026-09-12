import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_review_pr_dashboard.__main__ import get_parser, parse_args
from auto_review_pr_dashboard.config import (
    AI_CLI_FLAGS,
    SUPPORTED_AI_CLIS,
    RunConfig,
    UnsupportedAiCliError,
    get_ai_cli_family,
    get_ai_command,
    get_is_usage_limit,
    get_scoped_prompt,
)


class ConfigParityTest(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = Path.cwd()
        self.tmp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.tmp_dir.name)
        self.addCleanup(self.tmp_dir.cleanup)
        self.addCleanup(os.chdir, self.previous_cwd)

    def test_help_has_generic_usage_examples(self):
        help_text = get_parser().format_help()
        examples = (
            "auto-review-pr-dashboard codex --repo-url "
            "https://github.com/owner/repo --author <author-username> --dry-run",
            "auto-review-pr-dashboard codex --repo-url "
            "https://github.com/<owner>/<repo1> --repo-url "
            "https://github.com/<owner>/<repo2> --author <author-username> "
            "--reviewer <reviewer-username>",
            "auto-review-pr-dashboard codex --pr-url "
            "https://github.com/<owner>/<repo>/pull/<pr-number>",
            "auto-review-pr-dashboard --resume",
            "auto-review-pr-dashboard -l",
        )
        for example in examples:
            self.assertIn(example, help_text)
        self.assertNotIn("scott" + ".lan", help_text)
        self.assertNotIn("<revi" + "wer-username>", help_text)
        for text in (
            "prompt.md",
            "current working directory",
            "When prompt.md is absent, it is created with: code review",
            "the positional prompt is ignored",
            "Empty or whitespace-only files are rejected",
            "read once at startup",
            "--resume does not reread it",
            "Add any custom skill invocation to prompt.md itself",
            "/baseline-fe-code-review",
        ):
            self.assertIn(text, help_text)

    def test_family_and_command_shape(self):
        cases = [(family, family) for family in SUPPORTED_AI_CLIS]
        cases += [
            ("codex-personal", "codex"),
            ("codex-headroom", "codex"),
            ("claude-personal", "claude"),
            ("claude-lm-studio", "claude"),
            ("pi-preview", "pi"),
            ("opencode-dev", "opencode"),
        ]
        for ai_cli, family in cases:
            with self.subTest(ai_cli=ai_cli):
                self.assertEqual(get_ai_cli_family(ai_cli), family)
        for ai_cli in ("gemini", "codexx", "code x", ""):
            with self.subTest(ai_cli=ai_cli), self.assertRaises(UnsupportedAiCliError):
                get_ai_cli_family(ai_cli)
        command = get_ai_command(
            "codex-personal", "review", "owner/repo", "https://x/pull/7"
        )
        self.assertEqual(command[:4], ["bash", "-c", '"$0" "$@"', "codex-personal"])
        self.assertEqual(command[4:-1], AI_CLI_FLAGS["codex"])

    def test_scoped_prompt_contract(self):
        prompt = get_scoped_prompt(
            "review my open PRs", "owner/repo", "https://github.com/owner/repo/pull/7"
        )
        for text in (
            "review my open PRs",
            "owner/repo",
            "https://github.com/owner/repo/pull/7",
            "Do not ask any question",
        ):
            self.assertIn(text, prompt)
        for keyword in ("re-review", "force review", "review again", "重新 review"):
            self.assertNotIn(keyword, prompt.lower())

    def test_review_prompt_has_no_automatic_skill_invocation(self):
        ai_clis = (
            "codex",
            "codex-personal",
            "claude",
            "claude-personal",
            "pi",
            "pi-preview",
            "opencode",
            "opencode-dev",
        )
        user_prompt = "review my open PRs"
        repo = "owner/repo"
        url = "https://github.com/owner/repo/pull/7"
        original_scoped_prompt = get_scoped_prompt(user_prompt, repo, url)

        for ai_cli in ai_clis:
            with self.subTest(ai_cli=ai_cli):
                prompt = get_ai_command(ai_cli, user_prompt, repo, url)[-1]
                self.assertEqual(prompt, original_scoped_prompt)
                for text in (user_prompt, repo, url, "Do not ask any question"):
                    self.assertIn(text, prompt)
                for keyword in ("re-review", "force review", "review again", "重新 review"):
                    self.assertNotIn(keyword, prompt.lower())

    def test_prompt_file_is_the_only_prompt_source(self):
        prompt_file_content = (
            "/baseline-fe-code-review\n"
            "1. 著重找 bug\n"
            "2. 把缺少 testing 部分都列為 non-blocking"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_path = Path(tmp_dir) / "prompt.md"
            prompt_path.write_text(prompt_file_content, encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_dir)
                config = parse_args(
                    [
                        "codex",
                        "ignored positional prompt",
                        "--pr-url",
                        "https://github.com/o/r/pull/7",
                    ]
                )
                prompt_path.write_text("changed after startup", encoding="utf-8")
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.prompt, prompt_file_content)
        prompt = get_ai_command(
            config.ai_cli,
            config.prompt,
            "o/r",
            "https://github.com/o/r/pull/7",
        )[-1]
        self.assertTrue(prompt.startswith(prompt_file_content))
        self.assertIn("[auto-review-pr-dashboard scope]", prompt)

    def test_prompt_file_makes_positional_prompt_optional(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "prompt.md").write_text("/custom-review", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_dir)
                config = parse_args(
                    ["claude", "--pr-url", "https://github.com/o/r/pull/7"]
                )
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.prompt, "/custom-review")

    def test_prompt_file_validation_and_read_error(self):
        cases = (("", "must not be empty"), (" \n\t", "must not be empty"))
        for content, expected_error in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp_dir:
                (Path(tmp_dir) / "prompt.md").write_text(content, encoding="utf-8")
                previous_cwd = Path.cwd()
                stderr = StringIO()
                try:
                    os.chdir(tmp_dir)
                    with redirect_stderr(stderr), self.assertRaises(SystemExit):
                        parse_args(
                            ["codex", "--pr-url", "https://github.com/o/r/pull/7"]
                        )
                finally:
                    os.chdir(previous_cwd)
                self.assertIn(expected_error, stderr.getvalue())

        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "prompt.md").mkdir()
            previous_cwd = Path.cwd()
            stderr = StringIO()
            try:
                os.chdir(tmp_dir)
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    parse_args(
                        ["codex", "--pr-url", "https://github.com/o/r/pull/7"]
                    )
            finally:
                os.chdir(previous_cwd)
            self.assertIn("prompt.md could not be read", stderr.getvalue())

    def test_missing_prompt_file_is_created_and_used(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            prompt_path = Path(tmp_dir) / "prompt.md"
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_dir)
                config = parse_args(
                    ["codex", "--pr-url", "https://github.com/o/r/pull/7"]
                )
                self.assertEqual(prompt_path.read_bytes(), b"code review\n")
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.prompt, "code review\n")

    def test_resume_does_not_read_prompt_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            (Path(tmp_dir) / "prompt.md").write_text("", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_dir)
                self.assertIsNone(parse_args(["--resume"]))
            finally:
                os.chdir(previous_cwd)

    def test_usage_limit_detection_matrix(self):
        limited = [
            "Usage limit reached · resets 3pm",
            "usage limit reached|1732492800",
            "429 Too Many Requests",
            "rate_limit_error",
            "exceeded your quota",
            "credit balance too low",
            "Overloaded (529)",
        ]
        ordinary = (
            None,
            "",
            "failed to fetch PR diff",
            "panic: nil pointer",
            "command not found",
            "could not resolve to a Repository",
        )
        self.assertTrue(all(map(get_is_usage_limit, limited)))
        self.assertFalse(any(map(get_is_usage_limit, ordinary)))

    def test_exported_bash_function_receives_arguments_and_environment(self):
        env = os.environ | {
            "BASH_FUNC_codex-stub%%": (
                '() { echo "stub got: $*"; echo "profile=$CODEX_PROFILE"; }'
            ),
            "CODEX_PROFILE": "/tmp/codex-personal",
        }
        command = get_ai_command(
            "codex-stub", "review", "owner/repo", "https://x/pull/7"
        )
        result = subprocess.run(
            command, capture_output=True, text=True, env=env, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stub got: exec -s workspace-write", result.stdout)
        self.assertNotIn("review-pr-branches", result.stdout)
        self.assertIn("https://x/pull/7", result.stdout)
        self.assertIn("profile=/tmp/codex-personal", result.stdout)

    def test_valid_arguments(self):
        self.assertIsNone(parse_args(["--resume"]))
        self.assertIsNone(parse_args(["-l"]))
        self.assertIsNone(parse_args(["--list"]))

        config = parse_args(
            shlex.split("codex 'review my PRs' --repo-url o/r --author me")
        )
        self.assertEqual(config, RunConfig("codex", "code review\n", ["o/r"], ["me"]))

        config = parse_args(
            shlex.split(
                "claude-personal p --repo-url o/r --author me --interval 30 "
                "--pr-timeout 5 --cooldown 10 --dry-run"
            )
        )
        self.assertEqual(config.interval_min, 30)
        self.assertEqual(config.pr_timeout_min, 5)
        self.assertEqual(config.cooldown_min, 10)
        self.assertTrue(config.dry_run)

        config = parse_args(
            shlex.split(
                "codex review --repo-url o/r1 --repo-url o/r2 "
                "--pr-url https://github.com/o/r3/pull/7 --author alice "
                "--author bob --reviewer carol"
            )
        )
        self.assertEqual(config.repos, ["o/r1", "o/r2"])
        self.assertEqual(config.urls, ["https://github.com/o/r3/pull/7"])
        self.assertEqual(config.authors, ["alice", "bob"])
        self.assertEqual(config.reviewers, ["carol"])

        config = parse_args(
            shlex.split("codex review --pr-url https://github.com/o/r/pull/7")
        )
        self.assertEqual(config.urls, ["https://github.com/o/r/pull/7"])
        self.assertEqual(config.authors, [])

    def test_invalid_arguments_matrix(self):
        cases = (
            "--resume --dry-run",
            "--resume codex review --repo-url o/r --author me",
            "codex review --repo-url o/r --author me --resume",
            "-l --dry-run",
            "--list --resume",
            "codex review --repo-url o/r --author me -l",
            "gemini p --repo-url o/r --author me",
            "codex review",
            "codex review --repo-url o/r",
            "codex p --interval 0 --repo-url o/r --author me",
            "codex p --pr-timeout -1 --repo-url o/r --author me",
            "codex p --cooldown 0 --repo-url o/r --author me",
        )
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parse_args(shlex.split(argv))

    def test_scope_normalization_matrix(self):
        config = parse_args(
            shlex.split(
                "codex review --repo-url https://github.com/o/r2 "
                "--repo-url o/r --repo-url https://github.com/o/r.git --author me"
            )
        )
        self.assertEqual(config.repos, ["o/r2", "o/r"])
        invalid = (
            "--repo-url https://github.com/o/r/pull/7 --author me",
            "--pr-url https://github.com/o/r",
            "--pr-url o/r#7",
            "--pr-url nonsense",
        )
        for scope in invalid:
            with self.subTest(scope=scope), self.assertRaises(SystemExit):
                parse_args(["codex", "review", *shlex.split(scope)])
