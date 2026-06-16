"""queueファイルのパース/シリアライズのユニットテスト"""
import json
from pathlib import Path

import pytest

from src.services import ow_service

SAMPLE_QUEUE_CONTENT = """\
---
topic_id: 454
channel_code: AbCdEfGh
---

## T1 | pin引き継ぎUI実装 | in_progress
- worker: w-a / term_ref: iterm2:8CDF1801-ABCD / session: sess-1
- activity: 801

## T2 | データ移行スクリプト作成 | queued
"""

SAMPLE_QUEUE_CONTENT_FULL_FRONTMATTER = """\
---
topic_id: 454
orch_activity_id: 798
channel_code: AbCdEfGh
orch_cwd: /Users/babajunichi/workspace
last_seen_msg_id: 128
---

## T1 | pin引き継ぎUI実装 | in_progress
- worker: w-a / term_ref: iterm2:8CDF1801-ABCD / session: sess-1
- activity: 801

## T2 | データ移行スクリプト作成 | queued
"""

SAMPLE_QUEUE_CONTENT_NO_FRONTMATTER = """\
## T1 | pin引き継ぎUI実装 | in_progress
- worker: w-a / term_ref: iterm2:8CDF1801-ABCD / session: sess-1
- activity: 801

## T2 | データ移行スクリプト作成 | queued
"""


class TestParseQueueFile:
    def test_parses_basic_queue(self, tmp_path: Path):
        """基本的なqueueファイルをパースできる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 2

    def test_parses_task_fields(self, tmp_path: Path):
        """タスクのフィールドが正しくパースされる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        t1 = tasks[0]
        assert t1["task"] == "T1"
        assert t1["title"] == "pin引き継ぎUI実装"
        assert t1["status"] == "in_progress"
        assert t1["term_ref"] == "iterm2:8CDF1801-ABCD"

    def test_parses_queued_task(self, tmp_path: Path):
        """queuedステータスのタスクもパースできる"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        t2 = tasks[1]
        assert t2["task"] == "T2"
        assert t2["status"] == "queued"

    def test_returns_empty_for_nonexistent_file(self, tmp_path: Path):
        """存在しないファイルは空tupleを返す"""
        queue_file = tmp_path / "queue-t999.md"
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter == {}
        assert tasks == []

    def test_returns_empty_for_empty_file(self, tmp_path: Path):
        """空のファイルは空tupleを返す"""
        queue_file = tmp_path / "queue-t0.md"
        queue_file.write_text("", encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter == {}
        assert tasks == []

    def test_handles_pipe_in_title(self, tmp_path: Path):
        """タイトルに ' | ' が含まれていてもstatusが正しくパースされる"""
        content = "## T1 | fix: A | B問題 | in_progress\n- worker: w-a / term_ref: uuid-1 / session: s1\n"
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(content, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "fix: A | B問題"
        assert tasks[0]["status"] == "in_progress"

    def test_handles_spawning_status(self, tmp_path: Path):
        """spawningステータスのwrite-aheadエントリをパースできる"""
        content = "## T3 | new task | spawning\n- worker: w-b / term_ref: (pending) / session: (pending)\n"
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(content, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        assert len(tasks) == 1
        assert tasks[0]["status"] == "spawning"


class TestParseQueueFileFrontmatter:
    """エッジケース#1-#8: frontmatter対応テスト"""

    def test_edge_case_3_parses_frontmatter_with_tasks(self, tmp_path: Path):
        """EC#3: frontmatter付きqueueファイルのパース → frontmatter dictとtasks listのtupleが返る"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT_FULL_FRONTMATTER, encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        # frontmatterが正しくパースされる
        assert frontmatter["topic_id"] == 454
        assert frontmatter["orch_activity_id"] == 798
        assert frontmatter["channel_code"] == "AbCdEfGh"
        assert frontmatter["orch_cwd"] == "/Users/babajunichi/workspace"
        assert frontmatter["last_seen_msg_id"] == 128
        # タスクも正しくパースされる
        assert len(tasks) == 2
        assert tasks[0]["task"] == "T1"
        assert tasks[0]["status"] == "in_progress"

    def test_edge_case_4_no_frontmatter_returns_empty_dict(self, tmp_path: Path):
        """EC#4: frontmatterなしqueueファイル（旧形式）のパース → 空dict {}とtasks listが返る"""
        queue_file = tmp_path / "queue-t454.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT_NO_FRONTMATTER, encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter == {}
        assert len(tasks) == 2
        assert tasks[0]["task"] == "T1"

    def test_edge_case_5_partial_frontmatter(self, tmp_path: Path):
        """EC#5: frontmatterの一部フィールドが欠けている → 存在するフィールドのみdictに含まれる"""
        content = """\
---
topic_id: 99
channel_code: XyZwAbCd
---

## T1 | テストタスク | queued
"""
        queue_file = tmp_path / "queue-t99.md"
        queue_file.write_text(content, encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter["topic_id"] == 99
        assert frontmatter["channel_code"] == "XyZwAbCd"
        # 欠けたフィールドはキーが存在しない
        assert "orch_activity_id" not in frontmatter
        assert "orch_cwd" not in frontmatter
        assert "last_seen_msg_id" not in frontmatter
        assert len(tasks) == 1

    def test_edge_case_7a_empty_file_returns_empty_tuple(self, tmp_path: Path):
        """EC#7a: 空ファイルのパース → ({}, []) が返る"""
        queue_file = tmp_path / "queue-t0.md"
        queue_file.write_text("", encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter == {}
        assert tasks == []

    def test_edge_case_7b_nonexistent_file_returns_empty_tuple(self, tmp_path: Path):
        """EC#7b: 存在しないファイルのパース → ({}, []) が返る"""
        queue_file = tmp_path / "queue-t999.md"
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        assert frontmatter == {}
        assert tasks == []

    def test_edge_case_8_invalid_yaml_frontmatter_fallback(self, tmp_path: Path):
        """EC#8: frontmatterが不正なYAML → 空dict {}にフォールバックし、タスク部分は正常パース"""
        content = """\
---
topic_id: [unclosed bracket
channel_code: broken:yaml: value
---

## T1 | タスク | queued
"""
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(content, encoding="utf-8")
        frontmatter, tasks = ow_service._parse_queue_file(queue_file)
        # frontmatterは空dictにフォールバック
        assert frontmatter == {}
        # タスク部分は正常にパースされる
        assert len(tasks) == 1
        assert tasks[0]["task"] == "T1"
        assert tasks[0]["status"] == "queued"

    def test_parse_returns_tuple(self, tmp_path: Path):
        """_parse_queue_fileの戻り値がtupleであることを確認"""
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(SAMPLE_QUEUE_CONTENT, encoding="utf-8")
        result = ow_service._parse_queue_file(queue_file)
        assert isinstance(result, tuple)
        assert len(result) == 2
        frontmatter, tasks = result
        assert isinstance(frontmatter, dict)
        assert isinstance(tasks, list)


class TestWriteQueueSpawning:
    def test_creates_queue_file(self, tmp_path: Path):
        """queueファイルにspawning write-aheadエントリが作成される"""
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/tmp")
        queue_file = tmp_path / "queue-t454.md"
        assert queue_file.exists()
        content = queue_file.read_text(encoding="utf-8")
        assert "spawning" in content
        assert "T1" in content

    def test_appends_to_existing_file(self, tmp_path: Path):
        """既存のqueueファイルにspawning write-aheadエントリを追記する"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text("## T1 | 既存タスク | done\n")
        ow_service._write_queue_spawning(tmp_path, "100", "w-b", 2, "/tmp")
        content = queue_file.read_text(encoding="utf-8")
        assert "既存タスク" in content
        assert "T2" in content

    def test_edge_case_1_new_file_has_frontmatter(self, tmp_path: Path):
        """EC#1: 新規queueファイル作成時 → frontmatter（5フィールド）＋spawningエントリが書き込まれる"""
        ow_service._write_queue_spawning(
            tmp_path, "454", "w-a", 1, "/workspace/repo",
            task_title="queue統合タスク",
            model="opus",
            acceptance="テスト全通過",
            orch_activity_id=798,
            channel_code="AbCdEfGh",
            orch_cwd="/Users/babajunichi/workspace",
        )
        queue_file = tmp_path / "queue-t454.md"
        assert queue_file.exists()
        content = queue_file.read_text(encoding="utf-8")
        # frontmatterが含まれる
        assert content.startswith("---")
        assert "topic_id: 454" in content
        assert "orch_activity_id: 798" in content
        assert "channel_code: AbCdEfGh" in content
        assert "orch_cwd: /Users/babajunichi/workspace" in content
        assert "last_seen_msg_id: 0" in content
        # 正式フォーマットのspawningエントリ（title・status・各フィールド）が含まれる
        assert "## T1 | queue統合タスク | spawning" in content
        assert "- worker: w-a / term_ref: (pending) / session: (pending)" in content
        assert "- activity: 798" in content
        assert "- model: opus" in content
        assert "- cwd: /workspace/repo" in content
        assert "- acceptance: テスト全通過" in content
        assert "- note: spawning write-ahead" in content

    def test_edge_case_2_existing_file_frontmatter_unchanged(self, tmp_path: Path):
        """EC#2: 既存queueファイルへのspawning追記時 → frontmatterは変更されず、末尾にエントリが追記される"""
        queue_file = tmp_path / "queue-t100.md"
        original_frontmatter = """\
---
topic_id: 100
orch_activity_id: 555
channel_code: OrigCode
orch_cwd: /original/cwd
last_seen_msg_id: 50
---

## T1 | 既存タスク | done
"""
        queue_file.write_text(original_frontmatter, encoding="utf-8")
        ow_service._write_queue_spawning(
            tmp_path, "100", "w-b", 2, "/workspace/repo",
            task_title="新タスク",
            orch_activity_id=999,
            channel_code="NewCode",
            orch_cwd="/new/cwd",
        )
        content = queue_file.read_text(encoding="utf-8")
        # frontmatterは変更されない（元のchannelコードが残る）
        assert "channel_code: OrigCode" in content
        assert "orch_cwd: /original/cwd" in content
        assert "last_seen_msg_id: 50" in content
        # 新しいコードは追記されない
        assert "channel_code: NewCode" not in content
        # 既存タスクと新しいspawningエントリが両方含まれる
        assert "既存タスク" in content
        assert "## T2 | 新タスク | spawning" in content

    def test_edge_case_1_frontmatter_is_valid_yaml(self, tmp_path: Path):
        """EC#1: 新規ファイルに書かれたfrontmatterがYAMLとして正常にパースできる"""
        ow_service._write_queue_spawning(
            tmp_path, "454", "w-a", 1, "/workspace",
            orch_activity_id=798,
            channel_code="TestCode",
            orch_cwd="/Users/test/workspace",
        )
        queue_file = tmp_path / "queue-t454.md"
        fm, tasks = ow_service._parse_queue_file(queue_file)
        assert fm["topic_id"] == 454
        assert fm["orch_activity_id"] == 798
        assert fm["channel_code"] == "TestCode"
        assert fm["orch_cwd"] == "/Users/test/workspace"
        assert fm["last_seen_msg_id"] == 0
        # spawningエントリもパースされる
        assert len(tasks) == 1
        assert tasks[0]["status"] == "spawning"


class TestFormatQueueTaskEntry:
    """_format_queue_task_entry: 正式queueフォーマットのエントリ生成"""

    def test_basic_format(self):
        """ヘッダー行＋フィールド行が正式フォーマットで生成される"""
        entry = ow_service._format_queue_task_entry(
            task_n=1,
            title="タスク名",
            status="working",
            fields=[
                ("worker", "w-a / term_ref: iterm2:xxx / session: uuid"),
                ("activity", "801"),
                ("note", "実装中"),
            ],
        )
        assert entry == (
            "## T1 | タスク名 | working\n"
            "- worker: w-a / term_ref: iterm2:xxx / session: uuid\n"
            "- activity: 801\n"
            "- note: 実装中\n"
        )

    def test_field_order_preserved(self):
        """fieldsの順序がそのまま保持される"""
        entry = ow_service._format_queue_task_entry(
            task_n=2, title="t", status="queued",
            fields=[("b", "2"), ("a", "1"), ("c", "3")],
        )
        assert entry.index("- b: 2") < entry.index("- a: 1") < entry.index("- c: 3")

    def test_parseable_by_parse_queue_file(self, tmp_path: Path):
        """生成エントリが_parse_queue_fileでパース可能（ラウンドトリップ）"""
        entry = ow_service._format_queue_task_entry(
            task_n=7, title="round trip", status="done",
            fields=[("worker", "w-c / term_ref: t7 / session: s7")],
        )
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(entry, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        assert tasks[0]["task"] == "T7"
        assert tasks[0]["title"] == "round trip"
        assert tasks[0]["status"] == "done"
        assert tasks[0]["worker"] == "w-c"
        assert tasks[0]["term_ref"] == "t7"

    def test_newline_in_field_is_collapsed(self, tmp_path: Path):
        """フィールド値の改行は空白に畳まれ、エントリが複数行に分裂しない"""
        entry = ow_service._format_queue_task_entry(
            task_n=1, title="t", status="spawning",
            fields=[("acceptance", "条件1\n条件2\n条件3")],
        )
        # acceptanceは1行に収まる（改行が空白化）
        assert "- acceptance: 条件1 条件2 条件3\n" in entry
        # エントリ全体は「ヘッダー1行＋フィールド1行」= 2行のみ
        assert entry.count("\n") == 2

    def test_phantom_task_injection_is_prevented(self, tmp_path: Path):
        """フィールド値に '## T99 | ...' を改行付きで注入してもファントムタスクにならない"""
        malicious = "正当な条件\n## T99 | injected | hacked"
        entry = ow_service._format_queue_task_entry(
            task_n=1, title="t", status="spawning",
            fields=[("acceptance", malicious)],
        )
        queue_file = tmp_path / "queue-t1.md"
        queue_file.write_text(entry, encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(queue_file)
        # 注入されたT99はタスクとして認識されず、本物のT1のみ
        assert [t["task"] for t in tasks] == ["T1"]
        assert all(t["status"] != "hacked" for t in tasks)


class TestUpsertQueueTask:
    """_upsert_queue_task: queue状態更新の内部関数（追加/置換）"""

    def _entry(self, task_n, title, status, note):
        return ow_service._format_queue_task_entry(
            task_n=task_n, title=title, status=status,
            fields=[("worker", "w-a / term_ref: (pending) / session: (pending)"), ("note", note)],
        )

    def test_creates_new_file_with_frontmatter(self, tmp_path: Path):
        """新規ファイル: frontmatter＋エントリで初期化される"""
        fm = ow_service._build_queue_frontmatter("454", 798, "AbCdEfGh", "/cwd", 0)
        ow_service._upsert_queue_task(tmp_path, "454", 1, self._entry(1, "t1", "spawning", "n1"), fm)
        content = (tmp_path / "queue-t454.md").read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "topic_id: 454" in content
        assert "## T1 | t1 | spawning" in content

    def test_appends_new_task_preserving_existing(self, tmp_path: Path):
        """別T<n>の追記: 既存タスクのエントリは保持され、末尾に追記される"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(
            "## T1 | 既存タスク | done\n- worker: w-z / term_ref: t1 / session: s1\n- note: orch手書きメモ\n",
            encoding="utf-8",
        )
        ow_service._upsert_queue_task(tmp_path, "100", 2, self._entry(2, "新タスク", "spawning", "n2"))
        content = queue_file.read_text(encoding="utf-8")
        assert "## T1 | 既存タスク | done" in content
        assert "- note: orch手書きメモ" in content  # orch手編集が保持される
        assert "## T2 | 新タスク | spawning" in content

    def test_replaces_existing_task_block(self, tmp_path: Path):
        """同じT<n>の再upsert: 該当ブロックのみ置換され、重複追記されない"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(
            "## T1 | タスク | spawning\n- worker: w-a / term_ref: (pending) / session: (pending)\n- note: spawning write-ahead\n",
            encoding="utf-8",
        )
        ow_service._upsert_queue_task(tmp_path, "100", 1, self._entry(1, "タスク", "working", "実装中"))
        content = queue_file.read_text(encoding="utf-8")
        assert content.count("## T1 |") == 1  # 重複しない
        assert "## T1 | タスク | working" in content
        assert "spawning write-ahead" not in content  # 旧noteは消える
        assert "- note: 実装中" in content

    def test_replace_preserves_sibling_blocks_and_notes(self, tmp_path: Path):
        """T<n>置換時、前後の別タスクブロックとそのorch手書きnoteが保持される"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(
            "---\ntopic_id: 100\n---\n\n"
            "## T1 | first | done\n- worker: w-1 / term_ref: t1 / session: s1\n- note: T1メモ\n\n"
            "## T2 | second | working\n- worker: w-2 / term_ref: t2 / session: s2\n- note: 古いT2メモ\n\n"
            "## T3 | third | queued\n- worker: w-3 / term_ref: t3 / session: s3\n- note: T3メモ\n",
            encoding="utf-8",
        )
        ow_service._upsert_queue_task(tmp_path, "100", 2, self._entry(2, "second", "done", "新T2メモ"))
        fm, tasks = ow_service._parse_queue_file(queue_file)
        content = queue_file.read_text(encoding="utf-8")
        # frontmatter・前後ブロック・それぞれのnoteが保持される
        assert fm["topic_id"] == 100
        assert [t["task"] for t in tasks] == ["T1", "T2", "T3"]
        assert "- note: T1メモ" in content
        assert "- note: T3メモ" in content
        # T2は置換されている
        assert "## T2 | second | done" in content
        assert "- note: 新T2メモ" in content
        assert "古いT2メモ" not in content


class TestWriteQueueSpawningReSpawn:
    """再spawn時のspawningエントリ重複防止"""

    def test_respawn_replaces_not_duplicates(self, tmp_path: Path):
        """同一T<n>の再spawnでエントリが重複追記されず置換される"""
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/cwd", task_title="タスク")
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/cwd", task_title="タスク")
        content = (tmp_path / "queue-t454.md").read_text(encoding="utf-8")
        assert content.count("## T1 |") == 1

    def test_respawn_with_multiline_acceptance_no_block_residue(self, tmp_path: Path):
        """複数行＋'## '始まりの行を含むacceptanceでも再spawnでブロック残骸が出ない"""
        acc = "条件1を満たす\n## 補足見出し\n条件2を満たす"
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/cwd", task_title="タスク", acceptance=acc)
        ow_service._write_queue_spawning(tmp_path, "454", "w-a", 1, "/cwd", task_title="タスク", acceptance=acc)
        content = (tmp_path / "queue-t454.md").read_text(encoding="utf-8")
        _, tasks = ow_service._parse_queue_file(tmp_path / "queue-t454.md")
        # タスクはT1の1件のみ（補足見出しがファントムタスク化しない）
        assert [t["task"] for t in tasks] == ["T1"]
        # acceptanceは1行化されブロック残骸（独立した補足見出し行）が残らない
        assert "\n## 補足見出し\n" not in content


class TestOwStatusFrontmatter:
    """EC#6: ow_statusの戻り値にfrontmatter情報が含まれる"""

    def test_edge_case_6_status_includes_frontmatter(self, tmp_path: Path, monkeypatch):
        """EC#6: ow_statusの戻り値にfrontmatter情報（channel_code, last_seen_msg_id等）が含まれる"""
        queue_content = """\
---
topic_id: 100
orch_activity_id: 555
channel_code: TestChannel
orch_cwd: /workspace
last_seen_msg_id: 42
---

## T1 | タスクA | in_progress
- worker: w-a / term_ref: uuid-1 / session: s1
"""
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(queue_content, encoding="utf-8")

        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)

        def fake_relay_request(method, path, data=None):
            if "/presence" in path:
                return {"handles": ["orch", "w-a"]}
            return {"error": "unexpected"}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="100")

        # frontmatterフィールドが存在する
        assert "frontmatter" in result
        fm = result["frontmatter"]
        assert fm["channel_code"] == "TestChannel"
        assert fm["last_seen_msg_id"] == 42
        assert fm["orch_cwd"] == "/workspace"
        assert fm["topic_id"] == 100
        assert fm["orch_activity_id"] == 555

    def test_status_no_frontmatter_returns_empty_dict(self, tmp_path: Path, monkeypatch):
        """frontmatterなしのqueueファイルのとき、frontmatterキーは空dictを返す"""
        queue_content = """\
## T1 | タスクA | in_progress
- worker: w-a / term_ref: uuid-1 / session: s1
"""
        queue_file = tmp_path / "queue-t200.md"
        queue_file.write_text(queue_content, encoding="utf-8")

        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)

        def fake_relay_request(method, path, data=None):
            return {"handles": ["orch", "w-a"]}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="200")

        assert "frontmatter" in result
        assert result["frontmatter"] == {}


class TestOwStatusIntegration:
    """ケース#16: queueパース + /presence合成 → 統合ビューを返す"""

    def test_status_merges_queue_and_presence(self, tmp_path: Path, monkeypatch):
        """queue内のworkerとpresenceの突合でonlineフラグが付く"""
        queue_content = (
            "## T1 | タスクA | in_progress\n"
            "- worker: w-a / term_ref: uuid-1 / session: s1\n\n"
            "## T2 | タスクB | queued\n"
        )
        queue_file = tmp_path / "queue-t100.md"
        queue_file.write_text(queue_content, encoding="utf-8")

        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)

        def fake_relay_request(method, path, data=None):
            if "/presence" in path:
                return {"handles": ["orch", "w-a"]}
            return {"error": "unexpected"}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="100")

        assert len(result["tasks"]) == 2
        assert result["tasks"][0]["online"] is True
        assert result["presence"] == ["orch", "w-a"]
        assert result["summary"]["total_tasks"] == 2
        assert "w-a" in result["summary"]["online_workers"]

    def test_status_with_empty_queue(self, tmp_path: Path, monkeypatch):
        """queueファイルなし + presence取得 → タスク0件の統合ビュー"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)

        def fake_relay_request(method, path, data=None):
            return {"handles": ["orch"]}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id="999")
        assert result["tasks"] == []
        assert result["summary"]["total_tasks"] == 0


    def test_status_no_queue_dir_returns_empty(self, monkeypatch, tmp_path):
        """OW_QUEUE_DIR未設定かつデフォルトディレクトリが存在しない場合、tasks=[]を返す（初回起動シナリオ）"""
        nonexistent = tmp_path / "does_not_exist"
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", "")
        monkeypatch.setattr(ow_service, "_get_queue_dir", lambda: nonexistent)
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)
        monkeypatch.setattr(ow_service, "_relay_request", lambda *a, **k: {"handles": []})
        result = ow_service.ow_status(channel="ch", topic_id=None)
        assert result["tasks"] == []

    def test_status_multi_queue_uses_first_frontmatter(self, tmp_path: Path, monkeypatch):
        """topic_id未指定の全件走査時、ソート順で最初のfrontmatterを代表として返す"""
        q1 = tmp_path / "queue-t100.md"
        q1.write_text(
            "---\ntopic_id: 100\nchannel_code: ch100\n---\n\n"
            "## T1 | タスクA | done\n",
            encoding="utf-8",
        )
        q2 = tmp_path / "queue-t200.md"
        q2.write_text(
            "---\ntopic_id: 200\nchannel_code: ch200\n---\n\n"
            "## T1 | タスクB | in_progress\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))

        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda channel: True)

        def fake_relay_request(method, path, data=None):
            return {"handles": []}

        monkeypatch.setattr(ow_service, "_relay_request", fake_relay_request)

        result = ow_service.ow_status(channel="ch", topic_id=None)
        assert result["frontmatter"]["topic_id"] == 100
        assert result["frontmatter"]["channel_code"] == "ch100"
        assert len(result["tasks"]) == 2


class TestOwSpawnWorkerManualFallback:
    """ケース#14: アダプタ不在時にmanualフォールバックで起動コマンドを返す"""

    @pytest.fixture(autouse=True)
    def _stub_preflight(self, monkeypatch):
        """spawn前ヘルスチェック(T17) の relay/channel/presence を本物に当てずに通す。"""
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda c: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])

    def test_manual_fallback_when_ow_terminal_unset(self, tmp_path: Path, monkeypatch):
        """OW_TERMINAL未設定 → manual=True + commandキーを返す"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", topic_id="99", task_n=1,
        )
        assert result.get("manual") is True
        assert "command" in result
        assert "OW_ROLE=worker" in result["command"]
        assert "task_file" in result

    def test_manual_fallback_when_ow_terminal_manual(self, tmp_path: Path, monkeypatch):
        """OW_TERMINAL=manual → manual=True"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "manual")

        result = ow_service.ow_spawn_worker(
            alias="w-b", channel="ch2", cwd="/tmp", model="claude-opus-4-7",
            task_title="manual", acceptance="ok", task_n=2,
        )
        assert result.get("manual") is True
        assert "command" in result

    def test_worker_cmd_includes_add_dir_for_task_file_dir(self, tmp_path: Path, monkeypatch):
        """worker起動コマンドにtask_fileディレクトリへの--add-dirが含まれる（CWD外task fileの許可プロンプト抑制）"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", topic_id="99", task_n=1,
        )
        assert result.get("manual") is True
        cmd = result["command"]
        expected_task_dir = str(tmp_path / "tasks")
        assert f"--add-dir {expected_task_dir}" in cmd


class TestOwSpawnWorkerAdapter:
    """アダプタ経由でworkerを起動し、stdoutからterm_refを取得する"""

    @pytest.fixture(autouse=True)
    def _stub_preflight(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda c: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])

    def test_adapter_returns_term_ref_from_stdout(self, tmp_path: Path, monkeypatch):
        """アダプタのstdoutをterm_refとして使用する"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "iterm2")

        adapter_script = tmp_path / "iterm2.sh"
        adapter_script.write_text("#!/bin/bash\necho 'session-uuid-from-iterm2'\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", topic_id="99", task_n=1,
        )
        assert result.get("spawning") == "ok"
        assert result["term_ref"] == "session-uuid-from-iterm2"

    def test_adapter_empty_stdout_falls_back_to_uuid(self, tmp_path: Path, monkeypatch):
        """アダプタがstdoutに何も返さない場合はUUIDフォールバック"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "iterm2")

        adapter_script = tmp_path / "iterm2.sh"
        adapter_script.write_text("#!/bin/bash\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        result = ow_service.ow_spawn_worker(
            alias="w-b", channel="ch2", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=2,
        )
        assert result.get("spawning") == "ok"
        assert len(result["term_ref"]) > 0  # UUID fallback

    def test_adapter_failure_falls_back_to_manual(self, tmp_path: Path, monkeypatch):
        """アダプタが失敗した場合はmanualフォールバック"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "iterm2")

        adapter_script = tmp_path / "iterm2.sh"
        adapter_script.write_text("#!/bin/bash\nexit 1\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        result = ow_service.ow_spawn_worker(
            alias="w-c", channel="ch3", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=3,
        )
        assert result.get("manual") is True
        assert "adapter_error" in result

    def test_tmux_target_pane_appended_when_terminal_is_tmux(
        self, tmp_path: Path, monkeypatch
    ):
        """OW_TERMINAL=tmux かつ tmux_target_pane 指定時、adapter args の末尾に target_pane が追加される"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        adapter_script = tmp_path / "tmux.sh"
        adapter_script.write_text("#!/bin/bash\necho '%9'\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        captured_args: list[list[str]] = []
        real_run = ow_service.subprocess.run

        def capturing_run(args, **kwargs):
            captured_args.append(list(args))
            return real_run(args, **kwargs)

        monkeypatch.setattr(ow_service.subprocess, "run", capturing_run)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=1,
            tmux_target_pane="%0",
        )
        assert result.get("spawning") == "ok"
        # adapter_args は ["bash", "<script>", "spawn", cwd, worker_cmd, target_pane] の6要素
        assert len(captured_args[0]) == 6
        assert captured_args[0][-1] == "%0"

    def test_tmux_target_pane_omitted_when_none(
        self, tmp_path: Path, monkeypatch
    ):
        """OW_TERMINAL=tmux かつ tmux_target_pane=None のとき adapter args に target_pane が追加されない"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        adapter_script = tmp_path / "tmux.sh"
        adapter_script.write_text("#!/bin/bash\necho '%5'\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        captured_args: list[list[str]] = []
        real_run = ow_service.subprocess.run

        def capturing_run(args, **kwargs):
            captured_args.append(list(args))
            return real_run(args, **kwargs)

        monkeypatch.setattr(ow_service.subprocess, "run", capturing_run)

        result = ow_service.ow_spawn_worker(
            alias="w-b", channel="ch2", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=2,
        )
        assert result.get("spawning") == "ok"
        # tmux_target_pane未指定なので adapter_args は5要素のみ
        assert len(captured_args[0]) == 5

    def test_tmux_target_pane_ignored_when_terminal_is_not_tmux(
        self, tmp_path: Path, monkeypatch
    ):
        """OW_TERMINAL=iterm2 のとき tmux_target_pane 指定は無視され adapter args に追加されない"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "iterm2")

        adapter_script = tmp_path / "iterm2.sh"
        adapter_script.write_text("#!/bin/bash\necho 'iterm2-uuid'\n")
        adapter_script.chmod(0o755)
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        captured_args: list[list[str]] = []
        real_run = ow_service.subprocess.run

        def capturing_run(args, **kwargs):
            captured_args.append(list(args))
            return real_run(args, **kwargs)

        monkeypatch.setattr(ow_service.subprocess, "run", capturing_run)

        result = ow_service.ow_spawn_worker(
            alias="w-c", channel="ch3", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=3,
            tmux_target_pane="%0",
        )
        assert result.get("spawning") == "ok"
        # iterm2のため tmux_target_pane は無視され adapter_args は5要素、"%0"も含まれない
        assert len(captured_args[0]) == 5
        assert "%0" not in captured_args[0]


class TestOwSpawnWorkerEnsureChannel:
    """ow_spawn_worker: spawn前ヘルスチェック (T17) と ensure_channel 連携の動作確認"""

    def test_ensure_channel_called_before_spawn(self, tmp_path: Path, monkeypatch):
        """ensure_channelが成功すれば通常通りspawnが進む"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.delenv("OW_TERMINAL", raising=False)
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])

        called_channels = []

        def fake_ensure_channel(c):
            called_channels.append(c)
            return True

        monkeypatch.setattr(ow_service, "ensure_channel", fake_ensure_channel)

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="TestCh01", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=1,
        )
        assert called_channels == ["TestCh01"]
        assert result.get("manual") is True  # OW_TERMINAL未設定なのでmanual

    def test_ensure_channel_failure_returns_precondition_failed(
        self, tmp_path: Path, monkeypatch
    ):
        """ensure_channelが失敗すると spawn前ヘルスチェックで SPAWN_PRECONDITION_FAILED を返し、
        warningsに具体的なchannel名を含む。
        """
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda c: False)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])

        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="BadCh01", cwd="/tmp", model="claude-opus-4-7",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "SPAWN_PRECONDITION_FAILED"
        warnings = result["error"]["warnings"]
        assert any("BadCh01" in w for w in warnings)


class TestOwCloseWorkerTermRef:
    """ケース#17: term_ref(安定ID)で対象を特定"""

    def test_close_passes_term_ref_to_adapter(self, tmp_path: Path, monkeypatch):
        """アダプタにterm_refが正しく渡される"""
        adapter_script = tmp_path / "adapters" / "iterm2.sh"
        adapter_script.parent.mkdir(parents=True, exist_ok=True)
        adapter_script.write_text("#!/bin/bash\nexit 0\n")
        adapter_script.chmod(0o755)

        monkeypatch.setenv("OW_TERMINAL", "iterm2")
        monkeypatch.setattr(ow_service, "_get_adapter_path", lambda t: adapter_script)

        captured_args = []
        original_run = ow_service.subprocess.run

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            from unittest.mock import MagicMock
            return MagicMock(returncode=0)

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_close_worker(term_ref="8CDF1801-ABCD-1234")

        assert result.get("closed") is True
        assert "8CDF1801-ABCD-1234" in captured_args
        assert result["term_ref"] == "8CDF1801-ABCD-1234"

    def test_close_manual_fallback(self, monkeypatch):
        """アダプタ不在時はmanualフォールバック"""
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        result = ow_service.ow_close_worker(term_ref="some-uuid")
        assert result.get("manual") is True
        assert "some-uuid" in result.get("message", "")


class TestGetQueueDir:
    def test_returns_env_var_path(self, tmp_path: Path, monkeypatch):
        """OW_QUEUE_DIRが設定されている場合はその値をPathとして返す"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service._get_queue_dir()
        assert result == tmp_path

    def test_default_path_is_not_auto_memory(self, monkeypatch):
        """OW_QUEUE_DIR未設定の場合、~/.cc-memory/ow/orch を返す（auto-memory管理外）"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", "")
        result = ow_service._get_queue_dir()
        assert result == Path.home() / ".cc-memory" / "ow" / "orch"
        # auto-memoryが管理する~/.claude/projects/配下でないことを確認
        assert ".claude" not in str(result)

class TestSlugifyTaskTitle:
    def test_keeps_japanese(self):
        """日本語タイトルはそのままslugに残る"""
        assert ow_service._slugify_task_title("queue読み書き一元化") == "queue読み書き一元化"

    def test_uses_main_part_before_emdash(self):
        """「main — detail」構造はmain部分のみを採用する"""
        slug = ow_service._slugify_task_title("queue読み書き一元化 — ow_serviceがorch queueフォーマットを扱う")
        assert slug == "queue読み書き一元化"

    def test_collapses_spaces_and_unsafe_chars(self):
        """空白・パス危険文字は-に畳まれる"""
        assert ow_service._slugify_task_title("fix the/bug now") == "fix-the-bug-now"

    def test_truncates_to_max_len(self):
        """max_lenで切り詰め、末尾の-は除去される"""
        slug = ow_service._slugify_task_title("a" * 50, max_len=10)
        assert slug == "a" * 10

    def test_empty_title_returns_empty(self):
        """空タイトルは空文字列を返す"""
        assert ow_service._slugify_task_title("") == ""


class TestWriteTaskFile:
    def _parse(self, task_file: Path):
        fm, body = ow_service._parse_frontmatter(task_file.read_text(encoding="utf-8"))
        return fm, body

    def test_creates_markdown_task_file(self, tmp_path: Path):
        """task fileがmarkdown（frontmatter＋本文）として作成される"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path, task_n=1, alias="w-a", channel="AbCdEfGh",
            cwd="/tmp", model="claude-opus-4-7",
            task_title="テストタスク", acceptance="全テスト通過", context="背景説明",
            playbook="", timeout_min=60, activity_id=1, topic_id="10"
        )
        assert task_file.exists()
        assert task_file.suffix == ".md"
        fm, body = self._parse(task_file)
        # frontmatterに機械可読フィールドが入る
        assert fm["task"] == "T1"
        assert fm["alias"] == "w-a"
        assert fm["channel"] == "AbCdEfGh"
        assert fm["v"] == 1
        assert fm["activity_id"] == 1
        assert fm["permission_mode"] == "auto"
        # 本文にタイトル・acceptance・contextが入る
        assert "# T1: テストタスク" in body
        assert "## Acceptance" in body
        assert "全テスト通過" in body
        assert "## Context" in body
        assert "背景説明" in body
        # playbookは空なのでセクションが出ない
        assert "## Playbook" not in body

    def test_task_file_name_without_topic(self, tmp_path: Path):
        """topic_id未指定・slug空時はT{n}.md形式にフォールバックする"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path, task_n=5, alias="w-e", channel="ch1",
            cwd="/tmp", model="claude-opus-4-7",
            task_title="", acceptance="", context="", playbook="",
            timeout_min=30, activity_id=None, topic_id=None
        )
        assert task_file.name == "T5.md"

    def test_task_file_name_has_topic_prefix_and_slug(self, tmp_path: Path):
        """topic_id・タイトル指定時はt{topic_id}-T{n}-{slug}.md形式になる"""
        task_file = ow_service._write_task_file(
            task_dir=tmp_path, task_n=16, alias="w-a", channel="ch1",
            cwd="/tmp", model="opus",
            task_title="queue読み書き一元化 — ow_serviceがorch queueフォーマットを扱う",
            acceptance="", context="", playbook="",
            timeout_min=60, activity_id=821, topic_id="454"
        )
        assert task_file.name == "t454-T16-queue読み書き一元化.md"

    def test_task_file_topic_prefix_no_collision(self, tmp_path: Path):
        """同じtask_nでもtopicが異なれば別ファイルになる（衝突しない）"""
        f1 = ow_service._write_task_file(
            task_dir=tmp_path, task_n=1, alias="w-a", channel="ch1",
            cwd="/tmp", model="opus",
            task_title="topic454タスク", acceptance="", context="", playbook="",
            timeout_min=60, activity_id=1, topic_id="454"
        )
        f2 = ow_service._write_task_file(
            task_dir=tmp_path, task_n=1, alias="w-b", channel="ch2",
            cwd="/tmp", model="opus",
            task_title="topic100タスク", acceptance="", context="", playbook="",
            timeout_min=60, activity_id=2, topic_id="100"
        )
        assert f1.name == "t454-T1-topic454タスク.md"
        assert f2.name == "t100-T1-topic100タスク.md"
        assert f1 != f2
        assert "# T1: topic454タスク" in f1.read_text(encoding="utf-8")
        assert "# T1: topic100タスク" in f2.read_text(encoding="utf-8")

    def test_creates_parent_dirs(self, tmp_path: Path):
        """親ディレクトリが存在しなくても作成する"""
        nested_dir = tmp_path / "deep" / "nested" / "tasks"
        task_file = ow_service._write_task_file(
            task_dir=nested_dir, task_n=1, alias="w-a", channel="ch",
            cwd="/tmp", model="claude-opus-4-7",
            task_title="", acceptance="", context="", playbook="",
            timeout_min=60, activity_id=None, topic_id=None
        )
        assert task_file.exists()


class TestNormalizeAndValidateModel:
    """_normalize_and_validate_model のユニットテスト

    現行方針: claude-opus-4-7 のみ許可。sonnet・haiku・opus-4-8 は全て拒否。
    opus エイリアスはすべて 'claude-opus-4-7' に正規化される。
    """

    # --- sonnet 系: 全て拒否 ---

    def test_sonnet_shorthand_rejected(self):
        """'sonnet' 短縮形は拒否される"""
        model, err = ow_service._normalize_and_validate_model("sonnet")
        assert err is not None
        assert model == ""
        assert "sonnet" in err

    def test_sonnet_1m_rejected(self):
        """'sonnet[1m]' も拒否される"""
        model, err = ow_service._normalize_and_validate_model("sonnet[1m]")
        assert err is not None
        assert model == ""

    def test_sonnet_full_id_rejected(self):
        """'claude-sonnet-4-6' も拒否される"""
        model, err = ow_service._normalize_and_validate_model("claude-sonnet-4-6")
        assert err is not None
        assert model == ""

    def test_sonnet_full_id_with_1m_rejected(self):
        """'claude-sonnet-4-6[1m]' も拒否される"""
        model, err = ow_service._normalize_and_validate_model("claude-sonnet-4-6[1m]")
        assert err is not None
        assert model == ""

    # --- haiku 系: 全て拒否 ---

    def test_haiku_rejected(self):
        """'haiku' は拒否される"""
        model, err = ow_service._normalize_and_validate_model("haiku")
        assert err is not None
        assert model == ""
        assert "haiku" in err

    def test_haiku_full_id_rejected(self):
        """'claude-haiku-4-5-20251001' も拒否される"""
        model, err = ow_service._normalize_and_validate_model("claude-haiku-4-5-20251001")
        assert err is not None
        assert model == ""

    # --- opus 4.8: 拒否 ---

    def test_opus_4_8_rejected(self):
        """'claude-opus-4-8' は拒否される"""
        model, err = ow_service._normalize_and_validate_model("claude-opus-4-8")
        assert err is not None
        assert model == ""
        assert "opus 4.8" in err

    def test_opus_4_8_shorthand_rejected(self):
        """'opus-4-8' 短縮形も拒否される"""
        model, err = ow_service._normalize_and_validate_model("opus-4-8")
        assert err is not None
        assert model == ""

    # --- opus 4.7 系: 全て 'claude-opus-4-7' に正規化 ---

    def test_opus_shorthand_normalizes_to_claude_opus_4_7(self):
        """'opus' 短縮形は 'claude-opus-4-7' に正規化される"""
        model, err = ow_service._normalize_and_validate_model("opus")
        assert err is None
        assert model == "claude-opus-4-7"

    def test_opus_4_7_normalizes_to_claude_opus_4_7(self):
        """'opus-4-7' 短縮形も正規化される"""
        model, err = ow_service._normalize_and_validate_model("opus-4-7")
        assert err is None
        assert model == "claude-opus-4-7"

    def test_opus_4_7_with_1m_normalizes_to_claude_opus_4_7(self):
        """'opus-4-7[1m]' 等の [1m] 付きも正規化先は同じ"""
        model, err = ow_service._normalize_and_validate_model("opus-4-7[1m]")
        assert err is None
        assert model == "claude-opus-4-7"

    def test_opus_full_id_passthrough(self):
        """'claude-opus-4-7' はそのまま通る（正規化先と同一）"""
        model, err = ow_service._normalize_and_validate_model("claude-opus-4-7")
        assert err is None
        assert model == "claude-opus-4-7"

    # --- 未知のモデル: 拒否 ---

    def test_unknown_model_rejected(self):
        """未知のモデルIDも拒否される（claude-opus-4-7 のみ許可）"""
        model, err = ow_service._normalize_and_validate_model("claude-fable-5")
        assert err is not None
        assert model == ""


class TestOwSpawnWorkerModelValidation:
    """ow_spawn_worker のmodel validation/normalization 統合テスト"""

    @pytest.fixture(autouse=True)
    def _stub_preflight(self, monkeypatch):
        monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
        monkeypatch.setattr(ow_service, "ensure_channel", lambda c: True)
        monkeypatch.setattr(ow_service, "_get_presence", lambda c: [])
        monkeypatch.delenv("OW_TERMINAL", raising=False)

    def test_haiku_returns_invalid_model_error(self, tmp_path: Path, monkeypatch):
        """haiku を指定すると INVALID_MODEL エラーが返り spawn しない"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="haiku",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_MODEL"

    def test_opus_4_8_returns_invalid_model_error(self, tmp_path: Path, monkeypatch):
        """opus-4-8 を指定すると INVALID_MODEL エラーが返る"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-opus-4-8",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_MODEL"

    def test_sonnet_returns_invalid_model_error(self, tmp_path: Path, monkeypatch):
        """'sonnet' は拒否され INVALID_MODEL エラーが返る"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="sonnet",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_MODEL"

    def test_claude_sonnet_full_id_rejected(self, tmp_path: Path, monkeypatch):
        """'claude-sonnet-4-6' のフルIDも拒否される"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-sonnet-4-6",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_MODEL"

    def test_opus_shorthand_normalized_to_claude_opus_4_7(self, tmp_path: Path, monkeypatch):
        """'opus' 指定時、生成コマンドに 'claude-opus-4-7' に正規化される"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="opus",
            task_title="test", acceptance="done", task_n=1,
        )
        assert result.get("manual") is True
        assert "claude-opus-4-7" in result["command"]

    def test_claude_haiku_full_id_rejected(self, tmp_path: Path, monkeypatch):
        """'claude-haiku-4-5-20251001' のフルIDでも拒否される"""
        monkeypatch.setattr(ow_service, "OW_QUEUE_DIR", str(tmp_path))
        result = ow_service.ow_spawn_worker(
            alias="w-a", channel="ch1", cwd="/tmp", model="claude-haiku-4-5-20251001",
            task_title="test", acceptance="done", task_n=1,
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_MODEL"
