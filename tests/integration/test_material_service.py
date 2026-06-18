"""資材サービスの統合テスト"""
import os
import tempfile
import pytest
from src.db import get_connection, init_database
from src.services.activity_service import add_activity
from src.services.material_service import add_material, get_material, update_material
from src.services.search_service import get_by_id, get_by_ids


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def activity_id(temp_db):
    """テスト用アクティビティを作成してIDを返すフィクスチャ"""
    result = add_activity(
        title="Test Activity",
        description="Activity for material tests",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    return result["activity_id"]


class TestAddMaterial:
    """add_materialの統合テスト"""

    def test_add_material_success(self, temp_db):
        """title, content, tags, sourceを指定して資材を追加すると成功しmaterial_idが返る"""
        result = add_material(
            title="Test Material",
            content="# Test Content\n\nThis is a test material.",
            tags=["domain:test", "design"],
            source="テスト用データ",
        )

        assert "error" not in result
        assert result["material_id"] > 0

    def test_add_material_with_related(self, activity_id):
        """relatedを指定してリレーション付きで資材を作成できる"""
        result = add_material(
            title="Related Material",
            content="Content",
            tags=["domain:test"],
            source="テスト用データ",
            related=[{"type": "activity", "ids": [activity_id]}],
        )

        assert "error" not in result
        assert result["material_id"] > 0

    def test_add_material_empty_title(self, temp_db):
        """titleが空文字の場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="",
            content="Content",
            tags=["domain:test"],
            source="テスト用データ",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "title" in result["error"]["message"]

    def test_add_material_whitespace_title(self, temp_db):
        """titleが空白のみの場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="   ",
            content="Content",
            tags=["domain:test"],
            source="テスト用データ",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_add_material_empty_content(self, temp_db):
        """contentが空文字の場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="Title",
            content="",
            tags=["domain:test"],
            source="テスト用データ",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "content" in result["error"]["message"]

    def test_add_material_whitespace_content(self, temp_db):
        """contentが空白のみの場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="Title",
            content="   ",
            tags=["domain:test"],
            source="テスト用データ",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_add_material_no_tags(self, temp_db):
        """tagsが空配列の場合TAGS_REQUIREDエラーを返す"""
        result = add_material(
            title="Title",
            content="Content",
            tags=[],
            source="テスト用データ",
        )

        assert "error" in result
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_add_material_empty_source(self, temp_db):
        """sourceが空文字の場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="Title",
            content="Content",
            tags=["domain:test"],
            source="",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "source" in result["error"]["message"]

    def test_add_material_whitespace_source(self, temp_db):
        """sourceが空白のみの場合VALIDATION_ERRORを返す"""
        result = add_material(
            title="Title",
            content="Content",
            tags=["domain:test"],
            source="   ",
        )

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "source" in result["error"]["message"]

    def test_add_material_source_persisted(self, temp_db):
        """sourceを指定して作成した資材のget_materialレスポンスにsourceが含まれる"""
        created = add_material(
            title="Source Test",
            content="Content for source test",
            tags=["domain:test"],
            source="公式ドキュメント",
        )
        assert "error" not in created

        fetched = get_material(created["material_id"])
        assert fetched["source"] == "公式ドキュメント"

    def test_add_multiple_materials(self, temp_db):
        """複数の資材を追加するとそれぞれ異なるmaterial_idが割り当てられる"""
        result1 = add_material(
            title="Material 1",
            content="Content 1",
            tags=["domain:test"],
            source="テスト用データ",
        )
        result2 = add_material(
            title="Material 2",
            content="Content 2",
            tags=["domain:test"],
            source="テスト用データ",
        )

        assert "error" not in result1
        assert "error" not in result2
        assert result1["material_id"] != result2["material_id"]


class TestGetMaterial:
    """get_materialの統合テスト"""

    def test_get_material_success(self, temp_db):
        """get_materialでmaterial_id, title, content, source, tags, created_atが返る"""
        created = add_material(
            title="Get Test",
            content="Full content here",
            tags=["domain:test", "search"],
            source="コード調査",
        )
        material_id = created["material_id"]

        result = get_material(material_id)

        assert "error" not in result
        assert result["material_id"] == material_id
        assert result["title"] == "Get Test"
        assert result["content"] == "Full content here"
        assert result["source"] == "コード調査"
        assert "tags" in result
        assert "domain:test" in result["tags"]
        assert "search" in result["tags"]
        assert "created_at" in result
        # activity_idが含まれないこと
        assert "activity_id" not in result

    def test_get_material_has_hint(self, temp_db):
        """get_materialのレスポンスにhintフィールドが含まれる"""
        created = add_material(
            title="Hint Test",
            content="Content for hint test",
            tags=["domain:test"],
            source="テスト用データ",
        )

        result = get_material(created["material_id"])

        assert "error" not in result
        assert "hint" in result
        assert "snippet" in result["hint"]

    def test_get_material_not_found(self, temp_db):
        """存在しないmaterial_idでNOT_FOUNDエラーになる"""
        result = get_material(9999)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"


class TestGetByIdMaterial:
    """get_by_id / get_by_ids でmaterialを取得するテスト"""

    def test_get_by_id_material(self, temp_db):
        """get_by_idでmaterialのカタログ（material_id, title, tags, created_at）を取得できる"""
        created = add_material(
            title="ById Test",
            content="ById content",
            tags=["domain:test"],
            source="テスト用データ",
        )
        material_id = created["material_id"]

        result = get_by_id("material", material_id)

        assert "error" not in result
        assert result["type"] == "material"
        assert result["data"]["material_id"] == material_id
        assert result["data"]["title"] == "ById Test"
        assert "content" not in result["data"]  # カタログ形式: 全文なし
        assert result["data"]["tags"] == ["domain:test"]  # material自身のタグ
        # activity_idが含まれないこと
        assert "activity_id" not in result["data"]

    def test_get_by_id_material_not_found(self, temp_db):
        """存在しないmaterial_idでNOT_FOUNDエラーになる"""
        result = get_by_id("material", 9999)

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_get_by_ids_material(self, temp_db):
        """get_by_idsでmaterialを取得できる"""
        created = add_material(
            title="Batch Test",
            content="Batch content",
            tags=["domain:test"],
            source="テスト用データ",
        )
        material_id = created["material_id"]

        result = get_by_ids([{"type": "material", "id": material_id}])

        assert "error" not in result
        assert len(result["results"]) == 1
        assert result["results"][0]["type"] == "material"
        assert result["results"][0]["data"]["material_id"] == material_id

    def test_get_by_ids_mixed_types(self, activity_id):
        """get_by_idsでmaterialと他のtypeを混在して取得できる"""
        created = add_material(
            title="Mixed Test",
            content="Mixed content",
            tags=["domain:test"],
            source="テスト用データ",
            related=[{"type": "activity", "ids": [activity_id]}],
        )
        material_id = created["material_id"]

        result = get_by_ids([
            {"type": "material", "id": material_id},
            {"type": "activity", "id": activity_id},
        ])

        assert "error" not in result
        assert len(result["results"]) == 2
        # material
        assert result["results"][0]["type"] == "material"
        assert result["results"][0]["data"]["material_id"] == material_id
        # activity
        assert result["results"][1]["type"] == "activity"
        assert result["results"][1]["data"]["id"] == activity_id


class TestUpdateMaterial:
    """update_material integration tests"""

    def _create_material(self):
        """title='Original Title', content='Original content', source='テスト用データ'で資材を作成するヘルパー"""
        return add_material(
            title="Original Title",
            content="Original content",
            tags=["domain:test", "design"],
            source="テスト用データ",
        )

    def test_update_content(self, temp_db):
        """Updating content only succeeds"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="Updated content")

        assert "error" not in result
        assert result["material_id"] == material_id
        # レスポンス軽量化: material_idのみ
        assert "content" not in result
        assert "title" not in result
        assert "tags" not in result

    def test_update_title(self, temp_db):
        """Updating title only succeeds"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="Updated Title")

        assert "error" not in result
        assert result["material_id"] == material_id
        # レスポンス軽量化: material_idのみ
        assert "title" not in result
        assert "content" not in result
        assert "tags" not in result

    def test_update_both(self, temp_db):
        """Updating both content and title succeeds"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="New content", title="New Title")

        assert "error" not in result
        assert result["material_id"] == material_id
        # レスポンス軽量化: material_idのみ
        assert "content" not in result
        assert "title" not in result

    def test_update_neither_returns_validation_error(self, temp_db):
        """content, title, tags, sourceのいずれも指定しない場合VALIDATION_ERRORを返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "At least one" in result["error"]["message"]

    def test_update_not_found(self, temp_db):
        """Non-existent material_id returns NOT_FOUND"""
        result = update_material(9999, content="New content")

        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_update_empty_title(self, temp_db):
        """Empty title returns VALIDATION_ERROR"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "title" in result["error"]["message"]

    def test_update_persists_via_get_material(self, temp_db):
        """get_materialで更新が永続化されていることを確認"""
        created = self._create_material()
        material_id = created["material_id"]

        update_material(material_id, content="Persisted content", title="Persisted Title")

        fetched = get_material(material_id)
        assert fetched["title"] == "Persisted Title"
        assert fetched["content"] == "Persisted content"
        assert sorted(fetched["tags"]) == sorted(["design", "domain:test"])

    def test_update_empty_content(self, temp_db):
        """Empty content returns VALIDATION_ERROR"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "content" in result["error"]["message"]

    def test_update_tags(self, temp_db):
        """Updating tags only succeeds and replaces all tags"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, tags=["domain:new", "refactor"])

        assert "error" not in result
        assert result["material_id"] == material_id

        fetched = get_material(material_id)
        assert sorted(fetched["tags"]) == sorted(["domain:new", "refactor"])

    def test_update_tags_empty_list(self, temp_db):
        """Empty tags list returns TAGS_REQUIRED error"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, tags=[])

        assert "error" in result
        assert result["error"]["code"] == "TAGS_REQUIRED"

    def test_update_tags_with_content(self, temp_db):
        """Updating tags and content simultaneously succeeds"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="New content", tags=["domain:updated"])

        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "New content"
        assert fetched["tags"] == ["domain:updated"]

    def test_update_tags_none_preserves_existing(self, temp_db):
        """tags=Noneの場合、タグは変更されず作成時の値が保持される"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="New Title")
        assert "error" not in result

        fetched = get_material(material_id)
        assert sorted(fetched["tags"]) == sorted(["design", "domain:test"])

    def test_update_source(self, temp_db):
        """sourceのみを更新するとget_materialで新しいsource値が返る"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, source="公式ドキュメント")

        assert "error" not in result
        assert result["material_id"] == material_id

        fetched = get_material(material_id)
        assert fetched["source"] == "公式ドキュメント"

    def test_update_source_empty_string(self, temp_db):
        """sourceに空文字を指定するとVALIDATION_ERRORを返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, source="")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "source" in result["error"]["message"]

    def test_update_source_whitespace_only(self, temp_db):
        """sourceに空白のみを指定するとVALIDATION_ERRORを返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, source="   ")

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "source" in result["error"]["message"]

    def test_update_source_none_preserves_existing(self, temp_db):
        """source=Noneの場合、sourceは変更されず作成時の値が保持される"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="New Title")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["source"] == "テスト用データ"


class TestUpdateMaterialMode:
    """update_material の mode 引数 (overwrite/prepend/append) テスト"""

    def _create_material(self, content: str = "Original content"):
        return add_material(
            title="Mode Test Material",
            content=content,
            tags=["domain:test"],
            source="テスト用データ",
        )

    def test_mode_default_overwrites(self, temp_db):
        """mode未指定の場合、contentは上書きされる（後方互換）"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="brand new")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "brand new"

    def test_mode_overwrite_explicit(self, temp_db):
        """mode='overwrite'を明示指定した場合、contentは上書きされる"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="explicit overwrite", mode="overwrite")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "explicit overwrite"

    def test_mode_prepend_concatenates_new_then_existing(self, temp_db):
        """mode='prepend'の場合、'新content\\n\\n既存content'の順で結合される"""
        created = self._create_material(content="existing body")
        material_id = created["material_id"]

        result = update_material(material_id, content="new head", mode="prepend")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "new head\n\nexisting body"

    def test_mode_append_concatenates_existing_then_new(self, temp_db):
        """mode='append'の場合、'既存content\\n\\n新content'の順で結合される"""
        created = self._create_material(content="existing body")
        material_id = created["material_id"]

        result = update_material(material_id, content="appended tail", mode="append")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "existing body\n\nappended tail"

    def test_mode_invalid_returns_validation_error(self, temp_db):
        """mode に invalid な値を指定した場合、VALIDATION_ERROR を返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="x", mode="merge")  # type: ignore[arg-type]
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "mode" in result["error"]["message"]

    def test_mode_prepend_with_empty_new_content_returns_validation_error(self, temp_db):
        """mode='prepend' で新contentが空文字列の場合、VALIDATION_ERROR を返す（content空チェック）"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="", mode="prepend")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "content" in result["error"]["message"]

    def test_mode_append_with_whitespace_new_content_returns_validation_error(self, temp_db):
        """mode='append' で新contentが空白のみの場合、VALIDATION_ERROR を返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="   ", mode="append")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_mode_prepend_with_existing_empty_falls_back_to_overwrite(self, temp_db):
        """既存contentが空文字列の場合、prependでも区切りを挟まずoverwrite相当の動作になる"""
        created = self._create_material()
        material_id = created["material_id"]

        # 既存contentを空に書き換える（バリデーションを通常経路では通れないためDB直接操作）
        conn = get_connection()
        try:
            conn.execute("UPDATE materials SET content = ? WHERE id = ?", ("", material_id))
            conn.commit()
        finally:
            conn.close()

        result = update_material(material_id, content="only this", mode="prepend")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "only this"

    def test_mode_append_with_existing_empty_falls_back_to_overwrite(self, temp_db):
        """既存contentが空文字列の場合、appendでも区切りを挟まずoverwrite相当の動作になる"""
        created = self._create_material()
        material_id = created["material_id"]

        conn = get_connection()
        try:
            conn.execute("UPDATE materials SET content = ? WHERE id = ?", ("", material_id))
            conn.commit()
        finally:
            conn.close()

        result = update_material(material_id, content="only this", mode="append")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "only this"

    def test_mode_ignored_when_content_not_specified(self, temp_db):
        """contentを指定せずmodeのみ指定した場合、modeは無視されtitle更新は成功する"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="New Title", mode="prepend")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["title"] == "New Title"
        assert fetched["content"] == "Original content"  # 既存contentは変わらない

    def test_invalid_mode_ignored_when_content_not_specified(self, temp_db):
        """contentを指定しない場合、modeが不正な値でもバリデーションは実行されず更新は成功する"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, title="New Title", mode="invalid")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["title"] == "New Title"
        assert fetched["content"] == "Original content"

    def test_invalid_mode_rejected_when_content_specified(self, temp_db):
        """content指定時にmodeが不正な値の場合、VALIDATION_ERROR を返す"""
        created = self._create_material()
        material_id = created["material_id"]

        result = update_material(material_id, content="new", mode="invalid")
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "mode" in result["error"]["message"]

    def test_mode_prepend_multiline_preserves_structure(self, temp_db):
        """mode='prepend'で改行を含むcontentを結合しても、区切り '\\n\\n' が間に正しく入る"""
        created = self._create_material(content="line1\nline2")
        material_id = created["material_id"]

        result = update_material(material_id, content="head1\nhead2", mode="prepend")
        assert "error" not in result

        fetched = get_material(material_id)
        assert fetched["content"] == "head1\nhead2\n\nline1\nline2"
