"""MCPサーバーのメインエントリーポイント"""
import logging
import random
from fastmcp import FastMCP, Context
from fastmcp.server.dependencies import get_context
from typing import Literal, Optional, Union
from src.services import (
    topic_service,
    discussion_log_service,
    decision_service,
    search_service,
    activity_service,
    material_service,
    habit_service,
    relation_service,
    pin_service,
    retract_service,
    timeline_service,
    harness_service,
    ow_service,
)
from src.services.checkin_service import check_in as _check_in
from src.services.tag_service import search_tags as _search_tags, update_tag as _update_tag, collect_tag_notes_for_injection
from src.services.tag_analysis_service import analyze_tags as _analyze_tags
from src.db import get_connection
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# Instructions injected into the MCP server
RULES = """# cc-memory 利用ガイド

このツール群は、過去の会話コンテキスト（トピック・決定事項・ログ・アクティビティ・資材）の取得と記録を行います。
取得と記録の両輪を回すことで、ユーザーの繰り返し説明を防ぎ、次のAIセッションに文脈を引き継いだり、あなたの作業に必要な情報を入手できたりします。
この仕組みがうまく回るには、あなたの協力が不可欠です。
記録は自分のためだけでなく、次に来るエージェントのためのものです。責任を持って残してください。

## コンテキスト取得

最初の応答を組み立てる前に、関連する記録を取得してください。
これがこのツール群が存在する最も重要な理由です。ユーザーからの入力が単純でも省略しないでください。
ユーザーに意図を直接聞く前に検索、です。

## トピック管理

トピックは1つの関心事・問題・機能を表します。
タグで整理できるので、話題に応じてdomainやintentといったタグを積極的に付けてください。

後からの分割は困難なので、会話が進んで話題が具体化・分岐したら、その時点で新しいトピックを切ってください。
最初はざっくりしたスコープでも構いません。一時的な会話でもトピックは必要です。
スコープが小さくなってきたら、新しいトピックを作って紐づけるか、元のトピックのタイトルを更新してください。

## アクティビティ

セッションで何らかの作業を行う場合は、規模に関係なくアクティビティを作成してcheck-inしてください。
「SV（主語＋動詞）で何をするか表せるならアクティビティ」が判断基準です。

主なintent:タグの例: 議論（discuss）・設計（design）・作業（implement）。
アクティビティ名にはintentに応じた接頭辞をつけます: [議論]/[設計]/[作業]。
作業アクティビティには詳しい背景情報を書いてください。別のセッションが引き継ぐ可能性があります。

## 決定事項の記録

あなたとユーザーが何かに合意したら、`add_decisions`または`add_logs`で記録してください。
この記録は、将来あなたの代わりにやってくるAIセッションが一番頼りにするものです。
記録がなければ、同じ議論を繰り返すことになり、ユーザーとあなたの作業が無駄になります。

## ログの記録

決定事項は結論を記録しますが、そこに至る経緯は残りません。
ログはその経緯を保存するためのものです。
普段は見返されないかもしれませんが、いざ必要になったときに
そのトピックについての詳細な情報が失われていないことがとても重要です。

## 資材（material）

セッション中に生成された情報（ドラフト、分析結果、調査レポートなど）はセッション終了とともに消えます。
迷ったら保存してください。保存しすぎて困ることはありませんが、保存し忘れた情報は二度と戻りません。
要約や整理はせず、生データをそのまま保存してください。要約の過程で失われる詳細こそが将来価値を持ちます。
資材は決定事項と違って「双方の合意」が不要な成果物です。成果物が出た時点でユーザーに確認せず`add_material`してください。
`related`で関連するアクティビティやトピックとリレーションを張れます。

## リレーション

エンティティ同士を紐づけることで、関連する情報をまとめて辿れるようになります。
まとめて取得されるべきエンティティは、必ず紐づけを行ってください。紐づけがないことは文脈の喪失に等しいです。
記録時には`related`引数や`add_relation`で、関連するエンティティと積極的に紐づけてください。

## タグ

トピック・決定事項・ログ・アクティビティはすべてタグで整理されます。
記録時には必ずタグを付けてください。`domain:`タグは必須。アクティビティには`intent:`タグも必須です。素タグも積極的に付けてください。
namespace: `domain:`（関心領域）/ `intent:`（作業意図）/ 素タグ（キーワード）

### tag notes

タグには教訓・運用ルール（notes）を紐づけられます。
CLAUDE.mdのタグ版として機能し、そのタグに遭遇したとき（セッション内初回）にAIへ自動注入されます。
将来のエージェントのためを思い、必要だと思われる情報をどんどん記録してあげてください。

## 振る舞い（habits）

全セッション共通の行動ルールはhabitsとして記録できます。SessionStart時に全件注入されます。
ユーザーからもらったfeedbackのうち、タグやファイルに依存しない横断的なルールはauto-memoryではなくhabitsに記録してください。

---

あなたにはユーザーの壁打ち相手であり、記録係としての役割が期待されています。
ユーザーの発言は提案であり、決定ではありません。
懸念や代替案を積極的に提示し、双方が合意してから記録してください。

このツールがあなたとユーザーの仕事をより良くすることを願っています。Good luck!
"""


def build_instructions() -> str:
    """MCP instructionsを返す"""
    return RULES


def _maybe_inject_tag_notes(result: dict, tag_strings: list[str], mark: bool = True) -> dict:
    """結果dictにtag_notesを注入する（notes があれば）

    Note: always_inject_namespacesは渡さない（意図的）。
    intent:タグがこの経路で_injected_tagsに登録されるが、
    check_in経路はalways_inject_namespacesで常時注入が保証されるため問題ない。

    Args:
        mark: False の場合、_injected_tags を参照も更新もしない（読み取り経路用）。
    """
    try:
        ctx = get_context()
        session_id = ctx.session_id
    except RuntimeError:
        session_id = None
    conn = get_connection()
    try:
        notes = collect_tag_notes_for_injection(conn, tag_strings, session_id=session_id, mark=mark)
    finally:
        conn.close()
    if notes:
        result["tag_notes"] = notes
    return result


def _collect_result_tags(items: list[dict]) -> list[str]:
    """結果アイテムからユニークなタグを収集する"""
    tags: set[str] = set()
    for item in items:
        tags.update(item.get("tags", []))
    return sorted(tags)


# MCPサーバーを作成
mcp = FastMCP("cc-memory", instructions=build_instructions())

# セッション管理（HTTPモードで使用）
_session_manager = None

# ハーネス: 条件#4（整合性確認hint）のセッション別表示済みフラグ
_shown_consistency_hints: set[str] = set()


def get_session_manager():
    """現在のSessionManagerインスタンスを返す。HTTPモード以外ではNone。"""
    return _session_manager


# MCPツール定義
@mcp.tool()
def add_topic(
    title: str,
    description: str,
    tags: list[str],
    related: list[dict] | None = None,
) -> dict:
    """新しい議論トピックを追加する。

    tags: タグ配列(必須、1個以上)。domain:タグに加えて内容を表すタグも付けること。namespace: domain:(プロジェクト)/intent:(意図)/素タグ(キーワード)。例: ["domain:cc-memory", "intent:implement", "error-handling", "validation", "stdin"]
    related: 関連エンティティ（optional）。[{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...] 形式。複数エンティティを配列で同時紐付け可能。例: [{"type": "topic", "ids": [1, 2]}, {"type": "decision", "ids": [10]}]。作成と同時にリレーションを張る

    レスポンスに類似トピック(similar_topics)が含まれる場合がある。重複トピックの防止やリレーション追加の参考にすること。"""
    result = topic_service.add_topic(title, description, tags, related=related)
    if "error" not in result:
        _maybe_inject_tag_notes(result, tags)
    return result


@mcp.tool()
def add_logs(items: list[dict]) -> dict:
    """複数のログを一括追加する（最大10件）。

    items: ログ情報の配列。各要素は以下のキーを持つ:
        - topic_id (int, 必須): 対象トピックのID
        - content (str, 必須): 議論内容（マークダウン可）
        - title (str, optional): ログのタイトル。省略時はcontentの先頭行から自動生成
        - tags (list[str], optional): 追加タグ。省略時はtopicのタグを継承。内容を表すタグを積極的に追加すること。namespace: domain:(プロジェクト)/intent:(意図)/素タグ(キーワード)。例: ["intent:discuss", "migration", "breaking-change", "schema"]

    Returns: {created: [...], errors: [{index, error}]}
    """
    result = discussion_log_service.add_logs(items)
    if "error" not in result:
        # tag_notes: 全アイテムのタグをUNIONして1回注入
        all_tags = set()
        for item in items:
            if item.get("tags"):
                all_tags.update(item["tags"])
        if all_tags:
            _maybe_inject_tag_notes(result, list(all_tags))
    return result


@mcp.tool()
def add_decisions(items: list[dict], ctx: Context) -> dict:
    """複数の決定事項を一括記録する（最大10件）。

    items: 決定事項情報の配列。各要素は以下のキーを持つ:
        - topic_id (int, 必須): 関連するトピックのID
        - decision (str, 必須): 決定内容
        - reason (str, 必須): 決定の理由
        - title (str, optional): 決定の要点を表す1行。**付けることを強く推奨**。check-in・timeline・search等の一覧表示でdecision本文の代わりに見出しとして使われ、可読性が大きく上がる。省略時はdecision本文にfallbackする
        - tags (list[str], optional): 追加タグ。省略時はtopicのタグを継承。内容を表すタグを積極的に追加すること。namespace: domain:(プロジェクト)/intent:(意図)/素タグ(キーワード)。例: ["intent:design", "naming-convention", "backward-compat"]
        - propagate_to (dict, optional): 決定事項を注入先に伝搬する。
            - type: "habit" | "tag_note"
            - content: 伝搬先に書き込む文（decisionテキストとは別にエージェントが書き分ける）
            - tag: タグ文字列（type="tag_note"の場合のみ必須）

    Returns: {created: [...], errors: [{index, error}]}
        created各要素には related_decisions（同topic内の類似decision上位3件 [{id, title, distance}]）が付く。
        既存decisionとの矛盾・重複に気づくための導線。embeddingサーバー未起動時は空配列。
    """
    result = decision_service.add_decisions(items)
    if "error" not in result:
        # tag_notes: 全アイテムのタグをUNIONして1回注入
        all_tags = set()
        for item in items:
            if item.get("tags"):
                all_tags.update(item["tags"])
        if all_tags:
            _maybe_inject_tag_notes(result, list(all_tags))

        # ハーネス: 推奨行動hintを注入
        session_key = ctx.session_id or "__default__"
        shown = session_key in _shown_consistency_hints
        all_hints: list[str] = []
        seen_topics: set[int] = set()
        for item in items:
            tid = item.get("topic_id")
            if tid and tid not in seen_topics:
                seen_topics.add(tid)
                hints = harness_service.get_recommendations(tid, shown)
                all_hints.extend(hints)
        if all_hints:
            result["hints"] = list(dict.fromkeys(all_hints))
            if any(h == harness_service.HINT_CONSISTENCY_CHECK for h in all_hints):
                _shown_consistency_hints.add(session_key)
    return result


@mcp.tool()
def get_topics(
    tags: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """トピックを新しい順に取得する（ページネーション付き）。

    tags: タグ配列（optional）。指定時はAND条件でフィルタ。未指定時は全件返す。例: ["domain:cc-memory"]
    since: ISO日付文字列（例: "2026-03-10"）。この日付以降に作成されたトピックのみ返す
    until: ISO日付文字列。この日付以前に作成されたトピックのみ返す
    """
    result = topic_service.get_topics(tags, limit, offset, since, until)
    if "error" not in result:
        all_tags = _collect_result_tags(result.get("topics", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags, mark=False)
    return result


@mcp.tool()
def get_logs(
    entity_type: Literal["topic", "activity"],
    entity_id: int,
    start_id: Optional[int] = None,
    limit: int = 30,
    include_retracted: bool = False,
) -> dict:
    """
    指定エンティティの議論ログを取得する。

    Args:
        entity_type: エンティティタイプ（"topic" または "activity"）
        entity_id: 対象エンティティのID
        start_id: 取得開始位置のログID（ページネーション用）
        limit: 取得件数上限（最大30件）
        include_retracted: Trueのとき取り消し済みログも含める（デフォルトFalse）

    Returns:
        議論ログ一覧（各logにtags付き）
        entity_type == "activity" の場合はrelated topics経由でlogs集約
    """
    result = discussion_log_service.get_logs(entity_type, entity_id, start_id, limit, include_retracted=include_retracted)
    if "error" not in result:
        all_tags = _collect_result_tags(result.get("logs", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags, mark=False)
    return result


@mcp.tool()
def get_decisions(
    entity_type: Literal["topic", "activity"],
    entity_id: int,
    start_id: Optional[int] = None,
    limit: int = 30,
    include_retracted: bool = False,
) -> dict:
    """
    指定エンティティに関連する決定事項を取得する。

    Args:
        entity_type: エンティティタイプ（"topic" または "activity"）
        entity_id: 対象エンティティのID
        start_id: 取得開始位置の決定事項ID（ページネーション用）
        limit: 取得件数上限（最大30件）
        include_retracted: Trueのとき取り消し済み決定事項も含める（デフォルトFalse）

    Returns:
        決定事項一覧（各decisionにtags付き）
        entity_type == "activity" の場合はrelated topics経由でdecisions集約
    """
    result = decision_service.get_decisions(entity_type, entity_id, start_id, limit, include_retracted=include_retracted)
    if "error" not in result:
        all_tags = _collect_result_tags(result.get("decisions", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags, mark=False)
    return result


@mcp.tool()
def search(
    keyword: str | list[str],
    tags: Optional[list[str]] = None,
    entity_type: Optional[Literal["topic", "decision", "activity", "log", "material"]] = None,
    limit: int = 10,
    offset: int = 0,
    keyword_mode: str = "and",
    include_details: bool = False,
    domain: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_retracted: bool = False,
) -> dict:
    """
    キーワードで横断検索する。

    FTS5 trigramとベクトル検索のハイブリッド。RRFスコアで統合・ランキング。
    2文字以上のキーワードを指定する。
    配列で複数キーワードを渡すとAND検索（すべてを含む結果のみ返す）。
    keyword_mode="or"でOR検索（いずれかを含む結果を返す）。
    tagsでフィルタリング可能（AND結合）。未指定で全件検索。

    精度を上げるヒント: キーワードが曖昧なときは、先にsearch_tagsで
    関連タグを確認し、見つかったタグをtagsフィルタに指定すると効果的。
    特にdomain:タグでスコープを絞ると、無関係な結果を排除できる。

    Args:
        keyword: 検索キーワード（2文字以上）。配列で複数指定時はAND検索
        tags: タグフィルタ（AND条件。未指定=全件検索）
        entity_type: 検索対象の絞り込み（'topic', 'decision', 'activity', 'log', 'material'。未指定で全種類）
        limit: 取得件数上限（デフォルト10件、最大50件）
        offset: スキップ件数（デフォルト0）。ページネーション用
        keyword_mode: キーワード結合モード（"and" または "or"。デフォルト "and"）
        include_details: Trueのとき上位10件にdetailsを自動添付する（デフォルトFalse）
        domain: ドメインフィルタ。内部でtags=["domain:{domain}"]にマージされる
        date_after: 日付フィルタ（以降）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式
        date_before: 日付フィルタ（以前）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式
        include_retracted: Trueのとき取り消し済みのdecision/logも含める（デフォルトFalse）

    Returns:
        検索結果一覧（type, id, title, score, snippet, tags）
        scoreは0〜1に正規化された関連度スコア。1.0は全検索ソースで1位（理論最大）。
        片方のソースのみヒット時は最大0.5。目安: 0.4以上=高関連、0.15〜0.4=中関連、0.15未満=低関連。
        snippetは各typeの対応するソースカラムの先頭200文字（materialはtitle優先表示）。
        tagsはエンティティに紐づくタグ文字列のリスト。
        include_details=Trueの場合、上位10件にdetailsが追加される。
    """
    result = search_service.search(keyword, tags, entity_type, limit, offset, keyword_mode, include_details, domain, date_after, date_before, include_retracted=include_retracted)
    if "error" not in result and tags:
        _maybe_inject_tag_notes(result, tags)
    return result


@mcp.tool()
def get_by_ids(
    items: list[dict],
) -> dict:
    """
    search結果の詳細情報を取得する。

    searchツールで得られたtype + idペアを指定して、
    各アイテムの全文を返す。1件でも複数件でも使える。

    Args:
        items: 取得対象のリスト。各要素は {type: str, id: int}（最大20件）
               type: データ種別（'topic', 'decision', 'activity', 'log', 'material'）
               id: データのID

    Returns:
        取得結果（各アイテムの詳細情報）
    """
    result = search_service.get_by_ids(items)
    if "error" not in result:
        all_tags = []
        for item in result.get("results", []):
            if "data" in item:
                all_tags.extend(item["data"].get("tags", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags)
    return result


@mcp.tool()
def search_tags(
    query: str,
    namespace: Optional[str] = None,
    include_notes: bool = False,
    limit: int = 20,
) -> dict:
    """
    タグをキーワード検索する。

    タグ名の部分一致とベクトル検索のハイブリッドで、関連するタグを見つける。
    include_notes=Trueでnotesも確認できる。

    Args:
        query: 検索キーワード（タグ名部分一致 + ベクトル検索）
        namespace: namespaceフィルタ（"domain", "intent", ""。未指定で全タグ）
        include_notes: Trueのときnotesを返す（デフォルトFalse）
        limit: 取得件数上限（デフォルト20）

    Returns:
        検索結果（tags配列、各要素にscore付き）
    """
    return _search_tags(query, namespace, include_notes, limit)


@mcp.tool()
def update_tag(
    tag: str,
    notes: Optional[str] = None,
    canonical: Optional[str] = None,
    rename: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """
    既存タグの notes（教訓・運用ルール）、canonical（エイリアス先）、name（リネーム）、
    またはdescription（短い説明文）を更新する。

    notes / canonical / rename / description は相互排他（1つだけ指定可能）。少なくとも1つを指定する。

    notes: タグに紐づく教訓や運用ルールを記録する。CLAUDE.mdのタグ版として機能し、
    そのタグの文脈で作業するときに自動的にAIに注入される。上書き方式（全文置換）。

    canonical: エイリアス先タグを指定する。設定すると、tagがcanonicalのエイリアスになり、
    以降tagで記録・検索するとcanonical側のタグIDで解決される。
    設定時に既存の紐付け（topic_tags等4テーブル）をcanonical側に付け替える。
    この付け替えは設定時の1回のみで、canonical上書き時に旧付け替え分は戻らない。
    canonical=""で解除。連鎖（エイリアスのエイリアス）は禁止。
    notes付きタグはエイリアスにできない（先にnotesを除去すること）。

    rename: 新しいタグ名。namespace変更も可能（例: "hooks" → "domain:hooks"）。
    IDベースの参照なので紐付けはそのまま維持される。
    新名が既存タグと衝突する場合はエラー。

    description: タグの短い説明文（最大100文字）。空文字はNULLに正規化される。

    Args:
        tag: 対象タグ（例: "domain:cc-memory", "hooks"）
        notes: 教訓・運用ルールのテキスト（全文置換）
        canonical: エイリアス先タグ（""で解除）
        rename: 新しいタグ名（例: "domain:hooks"）
        description: タグの短い説明文（最大100文字）

    Returns:
        更新結果
    """
    return _update_tag(tag, notes=notes, canonical=canonical, rename=rename, description=description)


@mcp.tool()
def analyze_tags(
    domain: Optional[str] = None,
    include_domain_tags: bool = False,
    focus_tag: Optional[str] = None,
    min_usage: int = 2,
    top_n: int = 20,
) -> dict:
    """タグの共起分析を実行する。PMIで共起の重みを計算し、クラスタ検出・孤児タグ検出・重複候補検出を行う。

    Args:
        domain: domainフィルタ（例: "cc-memory"）。指定時はそのdomainに属するエンティティのみを分析対象にする
        include_domain_tags: Trueの場合、domain:タグも分析対象に含める（デフォルトFalse）
        focus_tag: 特定タグにフォーカス。指定時はco_occurrencesをそのタグを含むペアのみに絞る
        min_usage: 孤児判定の閾値。usage_countがこの値未満のタグを孤児とする（デフォルト2）
        top_n: co_occurrencesの返却件数上限（デフォルト20）

    Returns:
        co_occurrences: 共起ペア（PMI降順）
        clusters: PMI閾値ベースの連結成分クラスタ
        orphans: 使用頻度が低い孤児タグ
        suspected_duplicates: embedding類似度ベースの重複候補
    """
    return _analyze_tags(domain, include_domain_tags, focus_tag, min_usage, top_n)


@mcp.tool()
def add_activity(
    title: str,
    description: str,
    tags: list[str],
    related: list[dict] | None = None,
    check_in: bool = True,
) -> dict:
    """
    新しいアクティビティを追加する。デフォルトで作成後にcheck_inも実行する。

    典型的な使い方:
    - 作業アクティビティを作成: add_activity("○○機能を実装", "詳細説明...", ["domain:cc-memory", "intent:implement", "search", "ranking"])
    - トピック紐付け: add_activity("○○機能を実装", "詳細説明...", ["domain:cc-memory", "intent:implement"], related=[{"type": "topic", "ids": [123]}])
    - 複数関連: add_activity("...", "...", [...], related=[{"type": "topic", "ids": [1, 2]}, {"type": "activity", "ids": [3]}])
    - intent:implementは合意済みdecisionをrelateする: add_activity("...", "...", ["domain:cc-memory", "intent:implement"], related=[{"type": "decision", "ids": [10, 11]}])
    - check_inなしで作成: add_activity("○○機能を実装", "詳細説明...", ["domain:cc-memory", "intent:implement"], check_in=False)

    Args:
        title: アクティビティのタイトル
        description: アクティビティの詳細説明（必須）。スコアリングに活用されるため、以下の情報があれば記載を推奨: 締め切り、ブロッカー（自分が/外部）、影響度・緊急度
        tags: タグ配列（必須、1個以上）。domain:タグとintent:タグは必須。素タグも積極的に付けること。namespace: domain:(プロジェクト)/intent:(意図)/素タグ(キーワード)。例: ["domain:cc-memory", "intent:implement", "search", "ranking"]
        related: 関連エンティティ（optional）。[{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...] 形式。複数エンティティを配列で同時紐付け可能。例: [{"type": "topic", "ids": [1]}, {"type": "decision", "ids": [10, 11]}]。作成と同時にリレーションを張る。intent:implementタグを含む場合、relatedにtype='decision'のエントリを最低1件含めないとIMPLEMENT_WORKFLOW_GUARDエラーで弾かれる（議論・設計フェーズで合意したdecisionか、いきなりimplementする理由を記録したdecisionをrelateする）
        check_in: 作成後にcheck_inを実行するか（デフォルト: True）。Trueの場合、返り値にcheck_in_resultが含まれる

    Returns:
        作成されたアクティビティ情報（check_in=Trueの場合はcheck_in_resultにtag_notes等を含む）
    """
    result = activity_service.add_activity(
        title, description, tags, related=related, check_in=check_in,
    )
    if "error" not in result:
        # check_in=Trueの場合、check_in_resultにtag_notesが含まれるため
        # _maybe_inject_tag_notesは不要（二重注入防止）
        if not check_in:
            _maybe_inject_tag_notes(result, tags)
    return result


@mcp.tool()
def get_activities(
    tags: list[str] | None = None,
    status: str = "active",
    limit: int = 5,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """
    アクティビティ一覧を取得する（tagsでフィルタリング、statusでフィルタリング可能）。

    典型的な使い方:
    - 全アクティビティ確認: get_activities()
    - ドメイン指定: get_activities(["domain:cc-memory"])
    - 進行中のみ: get_activities(["domain:cc-memory"], status="in_progress")
    - 完了アクティビティの確認: get_activities(status="completed")
    - 最近1週間: get_activities(since="2026-03-09")

    ワークフロー位置: アクティビティ状況の確認時

    Args:
        tags: タグ配列（optional）。指定時はAND条件でフィルタ。未指定時は全件返す。例: ["domain:cc-memory"]
        status: フィルタするステータス（active/pending/in_progress/completed/snoozed/shelved、デフォルト: active）
                "active"はpending+in_progressの両方を返すエイリアス（snoozed/shelvedは含まない）
        limit: 取得件数上限（デフォルト: 5）
        since: ISO日付文字列（例: "2026-03-10"）。この日付以降に更新されたアクティビティのみ返す
        until: ISO日付文字列。この日付以前に更新されたアクティビティのみ返す

    Returns:
        アクティビティ一覧（total_countで該当ステータスの全件数を確認可能）
    """
    result = activity_service.get_activities(tags, status, limit, since, until)
    if "error" not in result:
        all_tags = _collect_result_tags(result.get("activities", []))
        if all_tags:
            _maybe_inject_tag_notes(result, all_tags, mark=False)
    return result


@mcp.tool()
def update_activity(
    activity_id: int,
    status: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """
    アクティビティのステータス・タイトル・説明・タグを更新する。

    典型的な使い方:
    - アクティビティ開始: update_activity(activity_id, status="in_progress")
    - アクティビティ完了: update_activity(activity_id, status="completed")
    - アクティビティを寝かせる: update_activity(activity_id, status="snoozed")
    - アクティビティを棚上げする: update_activity(activity_id, status="shelved")
    - タイトル変更: update_activity(activity_id, title="新しいタイトル")
    - 説明更新: update_activity(activity_id, description="新しい説明")
    - タグ変更: update_activity(activity_id, tags=["domain:cc-memory", "intent:implement"])

    ワークフロー位置: アクティビティ進行状況の更新時

    Args:
        activity_id: アクティビティID
        status: 新しいステータス（pending/in_progress/completed/snoozed/shelved）
        title: 新しいタイトル
        description: 新しい説明
        tags: 新しいタグ配列（指定時は全置換。1個以上必須）

    Returns:
        更新されたアクティビティ情報
    """
    return activity_service.update_activity(activity_id, status, title, description, tags)


@mcp.tool()
def add_material(
    title: str,
    content: str,
    tags: list[str],
    source: str,
    related: list[dict] | None = None,
) -> dict:
    """
    資材を追加する。独立エンティティとしてタグ付きで保存される。

    資材はセッション中の成果物・ドキュメントをDB保存する仕組み。
    search(entity_type="material")で検索でき、全文はget_materialで取得する2段階リード設計。
    決定事項と違って「双方の合意」が不要。成果物が出た時点でユーザーに確認せず呼ぶ。

    典型的な使い方:
    - 設計ドキュメントを保存: add_material("API設計書", "# API設計\n...", ["domain:cc-memory", "intent:design"], "コード調査")
    - 調査結果を保存: add_material("既存実装の調査結果", "## 調査結果\n...", ["domain:cc-memory", "調査"], "公式ドキュメント")
    - アクティビティと紐付け: add_material("設計書", "...", ["domain:cc-memory"], "ユーザー発言", related=[{"type": "activity", "ids": [123]}])

    Args:
        title: 資材のタイトル
        content: 資材の本文（マークダウン形式推奨）。先頭1-2文は内容の説明・要約を書くこと（check-in時にsnippetとして表示される）
        tags: タグ配列（必須、1個以上）。domain:タグに加えて内容を表すタグも付けること。namespace: domain:(プロジェクト)/intent:(意図)/素タグ(キーワード)
        source: データの出自。典型的なソース種類: ユーザー発言、公式ドキュメント、コード調査、計測結果、外部記事、チーム議事録など。事実と推論が混在する場合はcontent内で明示的に区別すること
        related: 関連エンティティ（optional）。[{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...] 形式。複数エンティティを配列で同時紐付け可能。例: [{"type": "activity", "ids": [123]}, {"type": "decision", "ids": [10]}]。作成と同時にリレーションを張る

    Returns:
        作成された資材情報（material_id, title, content, source, tags, created_at）
    """
    return material_service.add_material(title, content, tags, source, related=related)


@mcp.tool()
def update_material(
    material_id: int,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    mode: Literal["overwrite", "prepend", "append"] = "overwrite",
) -> dict:
    """
    既存の資材を更新する。content、title、tags、sourceを個別または同時に更新できる。

    contentはmodeで動作を選べる。"overwrite"（既定）は上書き、"prepend"は新content+区切り+既存content、
    "append"は既存content+区切り+新content。区切りは改行2つ("\n\n")。既存contentが空の場合はoverwrite相当。
    contentを指定しない場合（None）はmodeは無視される。
    tagsは全置換（指定時は既存タグを全削除して新しいタグに置き換える）。
    少なくとも1つのパラメータ（content/title/tags/source）を指定する必要がある。

    典型的な使い方:
    - 内容を改訂（上書き）: update_material(material_id=5, content="# 改訂版\n...")
    - 末尾に追記: update_material(material_id=5, content="## 追記\n...", mode="append")
    - 先頭に追記: update_material(material_id=5, content="## TL;DR\n...", mode="prepend")
    - タイトル変更: update_material(material_id=5, title="新しいタイトル")
    - タグ変更: update_material(material_id=5, tags=["domain:cc-memory", "design"])
    - ソース更新: update_material(material_id=5, source="公式ドキュメント")
    - 複数同時: update_material(material_id=5, content="...", title="...", tags=["..."])

    Args:
        material_id: 資材のID
        content: 新しい本文（optional）。先頭1-2文は内容の説明・要約を書くこと（check-inやsearchのsnippetに使われるため）
        title: 新しいタイトル（optional）
        tags: 新しいタグ配列（指定時は全置換。1個以上必須。optional）
        source: 新しいソース（optional）
        mode: content指定時の結合動作。"overwrite"=上書き(既定、後方互換)、"prepend"=新+"\n\n"+既存、"append"=既存+"\n\n"+新

    Returns:
        更新された資材情報
    """
    return material_service.update_material(material_id, content=content, title=title, tags=tags, source=source, mode=mode)


@mcp.tool()
def get_material(
    material_id: int,
) -> dict:
    """
    資材の全文を取得する。

    get_by_idsで取得したmaterial概要の詳細を取得する際に使う（2段階リードの後半）。

    Args:
        material_id: 資材のID

    Returns:
        資材の全文情報（material_id, title, content, source, tags, created_at）
    """
    return material_service.get_material(material_id)


@mcp.tool()
def check_in(
    activity_id: int,
) -> dict:
    """
    アクティビティにcheck-inする。関連情報を集約取得しsummaryを返す。

    既存アクティビティに関連する作業を始めるときに呼ぶ。
    tag_notes・資材カタログ・関連decisionsを一括取得し、
    statusがin_progress以外なら自動的にin_progressに更新する。
    summaryフィールドをそのまま出力すること。
    coverageが低い項目（目安: 50%未満）がある場合、特にlogsは議論の経緯を含むため優先的に取得を検討してください。

    Args:
        activity_id: アクティビティID

    Returns:
        check-in結果（coverage, activity, related_topics, related_activities, pinned, tag_notes, materials, recent_decisions, latest_log, logs, catalog, summary）
    """
    try:
        ctx = get_context()
        session_id = ctx.session_id
    except RuntimeError:
        session_id = None
    return _check_in(activity_id, session_id=session_id)



@mcp.tool()
def add_relation(
    source_type: Literal["topic", "activity", "material", "decision", "log"],
    source_id: int,
    targets: list[dict],
    relation_type: str = "related",
) -> dict:
    """
    エンティティ間のリレーションを追加する。

    典型的な使い方:
    - トピック同士を関連付け: add_relation("topic", 1, [{"type": "topic", "ids": [2, 3]}])
    - アクティビティとトピックを関連付け: add_relation("activity", 10, [{"type": "topic", "ids": [1]}])
    - 資材とアクティビティを関連付け: add_relation("material", 5, [{"type": "activity", "ids": [10]}])
    - 決定事項とトピックを関連付け: add_relation("decision", 1, [{"type": "topic", "ids": [1]}])
    - 複数タイプを一度に: add_relation("topic", 1, [{"type": "topic", "ids": [2]}, {"type": "activity", "ids": [10, 11]}])
    - 依存関係を追加: add_relation("activity", 1, [{"type": "activity", "ids": [2]}], relation_type="depends_on")
    - 上書き関係を追加: add_relation("decision", 2, [{"type": "decision", "ids": [1]}], relation_type="supersedes")

    Args:
        source_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        source_id: 起点エンティティのID
        targets: ターゲットリスト [{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...]
        relation_type: リレーションタイプ（"related", "depends_on", or "supersedes"）。
            depends_onはactivity同士のみ、supersedesはdecision同士のみ有効。

    Returns:
        成功時: {"added": int}（実際に追加された件数。重複はカウントしない）
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    return relation_service.add_relation(source_type, source_id, targets, relation_type)


@mcp.tool()
def remove_relation(
    source_type: Literal["topic", "activity", "material", "decision", "log"],
    source_id: int,
    targets: list[dict],
    relation_type: str = "related",
) -> dict:
    """
    エンティティ間のリレーションを削除する。

    典型的な使い方:
    - 関連リレーション削除: remove_relation("topic", 1, [{"type": "topic", "ids": [2]}])
    - 依存関係削除: remove_relation("activity", 1, [{"type": "activity", "ids": [2]}], relation_type="depends_on")
    - 上書き関係削除: remove_relation("decision", 2, [{"type": "decision", "ids": [1]}], relation_type="supersedes")

    Args:
        source_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        source_id: 起点エンティティのID
        targets: ターゲットリスト [{"type": "topic"|"activity"|"material"|"decision"|"log", "ids": [int, ...]}, ...]
        relation_type: リレーションタイプ（"related", "depends_on", or "supersedes"）。
            depends_onはactivity同士のみ、supersedesはdecision同士のみ有効。

    Returns:
        成功時: {"removed": int}（実際に削除された件数）
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    return relation_service.remove_relation(source_type, source_id, targets, relation_type)


@mcp.tool()
def get_map(
    entity_type: Literal["topic", "activity", "material", "decision", "log"],
    entity_id: int,
    min_depth: int = 0,
    max_depth: int = 2,
) -> dict:
    """
    リレーショングラフを走査し、到達可能エンティティのカタログを返す。

    再帰的にリレーションを辿り、指定深度範囲のエンティティをカタログ形式で返す。
    decision/logノードはグラフ走査の経由ノードとして使用するが、返却カタログにはtopic/activity/materialのみ含める。
    check-in時の2次カタログと同じロジックを使用。

    Args:
        entity_type: 起点エンティティのタイプ（"topic", "activity", "material", "decision", or "log"）
        entity_id: 起点エンティティのID
        min_depth: 最小深度（デフォルト: 0。0=起点自身を含む）
        max_depth: 最大深度（デフォルト: 2、上限: 10）

    Returns:
        成功時: {"entities": [{"type", "id", "title", "tags", "depth"}, ...], "total_count": int}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    return relation_service.get_map(entity_type, entity_id, min_depth, max_depth)


@mcp.tool()
def add_habit(content: str) -> dict:
    """エージェントの振る舞いを登録する。check-in時に自動注入され、以降の行動に反映される。"覚えといて"と言われた行動ルールはここに登録する"""
    return habit_service.add_habit(content)


@mcp.tool()
def get_habits() -> dict:
    """登録済みの振る舞い一覧を取得する"""
    return habit_service.get_habits()


@mcp.tool()
def update_habit(habit_id: int, content: Optional[str] = None, active: Optional[bool] = None) -> dict:
    """振る舞いを更新する。active=Falseで無効化、active=Trueで再有効化"""
    return habit_service.update_habit(habit_id, content=content, active=active)


@mcp.tool()
def add_pin(
    source_type: Literal["tag", "activity", "topic", "decision", "log", "material"],
    source_ref: Union[int, str],
    target_type: Literal["tag", "activity", "topic", "decision", "log", "material"],
    target_ref: Union[int, str],
) -> dict:
    """pinを追加する（source → target）。

    pinはsourceエンティティからtargetエンティティへの関係として記録される。
    check-in時にsourceに対応するpinのtargetが自動注入される。

    pin基準: 「これを知らずに着手したら間違った方向に進む」レベルの情報。

    pinすべき例:
    - 方向転換を記録したログ（以前の方針と異なる判断をした経緯）
    - プロジェクトの根幹に関わるdecision（アーキテクチャ選定、命名規約など）
    - 必読のmaterial（設計ドキュメント、仕様書など）

    pinしない例:
    - 進捗報告ログ（読まなくても方向を間違えない）
    - 独立した小さな決定（他の作業に影響しない）
    - 一時的な調査メモ（役目を終えた情報）

    source/target の種別は tag / activity / topic / decision / log / material のいずれか。
    tagのrefはID（整数）またはnamespace:name形式の文字列（例: "domain:cc-memory"）で指定できる。
    それ以外の種別のrefはIDを整数で指定する。

    重複追加は冪等（エラーにならない）。自己参照（source==target）は拒否される。
    source/targetが存在しない場合はNOT_FOUNDエラーを返す。

    Args:
        source_type: 起点エンティティ種別（"tag" | "activity" | "topic" | "decision" | "log" | "material"）
        source_ref: 起点エンティティのID（int）またはtag名文字列（tag種別のみ）
        target_type: 終点エンティティ種別（"tag" | "activity" | "topic" | "decision" | "log" | "material"）
        target_ref: 終点エンティティのID（int）またはtag名文字列（tag種別のみ）

    Returns:
        成功時: {"source_type": str, "source_id": int, "target_type": str, "target_id": int}
        失敗時: {"error": {"code": str, "message": str}}
    """
    return pin_service.add_pin(source_type, source_ref, target_type, target_ref)


@mcp.tool()
def remove_pin(
    source_type: Literal["tag", "activity", "topic", "decision", "log", "material"],
    source_ref: Union[int, str],
    target_type: Literal["tag", "activity", "topic", "decision", "log", "material"],
    target_ref: Union[int, str],
) -> dict:
    """pinを削除する（source → target）。

    unpin基準: 「もう知らなくてもいい状態になったか」。

    add_pinで追加したpinを削除する。対象pinが存在しない場合はremoved=0を返す（エラーにならない）。
    tag refを文字列で渡して該当tagが存在しなかった場合も removed=0 を返す（冪等）。

    tagのrefはID（整数）またはnamespace:name形式の文字列（例: "domain:cc-memory"）で指定できる。

    Args:
        source_type: 起点エンティティ種別（"tag" | "activity" | "topic" | "decision" | "log" | "material"）
        source_ref: 起点エンティティのID（int）またはtag名文字列（tag種別のみ）
        target_type: 終点エンティティ種別（"tag" | "activity" | "topic" | "decision" | "log" | "material"）
        target_ref: 終点エンティティのID（int）またはtag名文字列（tag種別のみ）

    Returns:
        成功時: {"removed": int}（実際に削除された件数）
        失敗時: {"error": {"code": str, "message": str}}
    """
    return pin_service.remove_pin(source_type, source_ref, target_type, target_ref)


@mcp.tool()
def retract(entity_type: Literal["decision", "log"], ids: list[int], undo: bool = False) -> dict:
    """決定事項やログを取り消す（論理削除）。取り消し済みエンティティは検索・取得でデフォルト除外される。

    Args:
        entity_type: "decision" | "log"
        ids: 対象エンティティのIDリスト
        undo: True=取り消しを元に戻す（un-retract）、False=取り消す（retract）
    """
    return retract_service.retract(entity_type, ids, undo)


@mcp.tool()
def get_timeline(
    topic_id: int | None = None,
    activity_id: int | None = None,
    entity_types: list[Literal["decision", "log", "material"]] | None = None,
    before: str | None = None,
    limit: int = 50,
    order: str = "desc",
) -> dict:
    """トピックまたはアクティビティに紐づくdecision・log・materialを時系列で返す。

    Args:
        topic_id: トピックID（activity_idと排他）
        activity_id: アクティビティID（topic_idと排他）
        entity_types: 取得するエンティティ型のリスト（"decision","log","material"のサブセット、未指定で全型）
        before: ページネーション用カーソル（ISO 8601形式のcreated_at）
        limit: 取得件数上限（デフォルト50、最大100）
        order: ソート方向（"desc"または"asc"、デフォルト"desc"）
    """
    return timeline_service.get_timeline(
        topic_id=topic_id, activity_id=activity_id,
        entity_types=entity_types, before=before,
        limit=limit, order=order,
    )


@mcp.tool()
def get_config() -> dict:
    """現在の設定値を返す。スキルが環境変数ベースの設定を参照するために使用する。"""
    from src import config
    return {
        "heartbeat_timeout": config.HEARTBEAT_TIMEOUT_MINUTES,
        "in_progress_limit": config.IN_PROGRESS_LIMIT,
        "pending_limit": config.PENDING_LIMIT,
        "recency_decay_rate": config.RECENCY_DECAY_RATE,
        "sync_disable_retrospective": config.SYNC_DISABLE_RETROSPECTIVE,
        "sync_policy": config.SYNC_POLICY,
        "snapshot_interval_hours": config.SNAPSHOT_INTERVAL_HOURS,
        "snapshot_max_count": config.SNAPSHOT_MAX_COUNT,
        "snapshot_anomaly_threshold": config.SNAPSHOT_ANOMALY_THRESHOLD,
    }


@mcp.tool()
def roll_dice(sides: int = 10) -> dict:
    """指定面数のダイスを振る。デフォルト1d10。"""
    return {"result": random.randint(1, sides)}


# ----------------------------
# ow（orch/worker）ツール群
# ----------------------------


@mcp.tool()
def ow_send(
    channel: str,
    handle: str,
    body: dict,
    needs_reply: bool = False,
    in_reply_to: int | None = None,
) -> dict:
    """ow channelにメッセージを送信する。

    bodyはow固有JSONを格納するdict（{"v":1, "kind":"command"|"event", ...}）。
    4xx即失敗、5xx/接続断のみ3回指数バックオフ。

    Args:
        channel: channelコード
        handle: 送信者handle（例: "orch", "w-a"）
        body: ow固有JSON。relayのbody内に格納される
        needs_reply: 返信を期待するか（デフォルト: False）
        in_reply_to: 返信先のmsg_id（optional）

    Returns:
        成功時: {"msg_id": int}
        失敗時: {"error": {...}}
    """
    return ow_service.ow_send(channel, handle, body, needs_reply, in_reply_to)


@mcp.tool()
def ow_history(channel: str, since: int = 0, limit: int = 100) -> dict:
    """ow channelの履歴を取得する。受信処理の本体。

    since自身を含まない（msg_id > since）。
    SSEは起床信号専用。起床後はこのツールで未処理メッセージを全件pull。

    Args:
        channel: channelコード
        since: このmsg_idより大きいものを返す（0=全件）
        limit: 最大取得件数（デフォルト: 100）

    Returns:
        {"messages": [{"msg_id": int, "handle": str, "body": dict, ...}, ...]}
        失敗時: {"error": {...}}
    """
    return ow_service.ow_history(channel, since, limit)


@mcp.tool()
def ow_spawn_worker(
    alias: str,
    channel: str,
    cwd: str,
    model: str,
    task_title: str = "",
    acceptance: str = "",
    context: str = "",
    playbook: str = "",
    timeout_min: int = 60,
    activity_id: int | None = None,
    topic_id: str | None = None,
    task_n: int = 1,
    tmux_target_pane: str | None = None,
    effort: str | None = None,
) -> dict:
    """workerセッションを起動する。

    処理順: queueへspawning write-ahead → task file書き出し → アダプタ起動 → 安定ID返却。
    relay疎通確認・起動（ensure-server処理）も内包。permission_modeはautoに固定。

    OW_TERMINAL環境変数でアダプタを選択（iterm2/tmux/manual。manualは起動コマンド表示のフォールバック）。
    OW_TERMINAL=tmuxのとき、tmux_target_pane（呼び出し元のTMUX_PANE）を渡すと、その
    paneと同じwindow内にworker paneを分割表示できる（最初は右に30%水平、以降は最新
    worker paneを垂直分割）。未指定時は従来の `ow-workers` 別sessionに新windowで起動する。

    Args:
        alias: workerのhandle（例: "w-a"）
        channel: channelコード
        cwd: workerの作業ディレクトリ
        model: 使用モデル。claude-opus-4-7 のみ許可。sonnet/haiku/opus-4-8 はバリデーションで拒否
        task_title: タスクタイトル
        acceptance: 完了条件
        context: タスクコンテキスト
        playbook: プレイブック抜粋（assignのplaybookフィールド）
        timeout_min: タイムアウト（分、デフォルト: 60）
        activity_id: 対応するアクティビティID（optional）
        topic_id: 対応するトピックID（optional）
        task_n: タスク番号（T<n>）
        tmux_target_pane: tmux分割表示用の基準pane ID（例: "%0"）。クライアント側で
            os.environ['TMUX_PANE']を読んで渡す。MCPサーバープロセスのenvは起動時に
            フリーズするためサーバー側で参照できない。
        effort: 思考worker（深い議論・設計検討・調査向けworker）として起動するなら
            `"high"` / `"xhigh"` / `"max"` / `"ultrathink"` のいずれかを指定する
            （デフォルト: None=通常worker）。指定時は task_file 本文に正規綴り
            `ultrathink` マーカーセクションが挿入され、frontmatter にも
            `effort: <値>` が残る。OW_TERMINAL=tmux のときは split-pane ではなく
            `tmux new-window` で別タブに開く。role は worker のまま。対応activity
            には `intent:thinking` タグを別途付与すること。

    Returns:
        成功時: {"term_ref": str, "task_file": str, "spawning": "ok", "alias": str}
        manualフォールバック時: {"command": str, "manual": True, "task_file": str, "alias": str}
        失敗時: {"error": {...}}
    """
    return ow_service.ow_spawn_worker(
        alias=alias,
        channel=channel,
        cwd=cwd,
        model=model,
        task_title=task_title,
        acceptance=acceptance,
        context=context,
        playbook=playbook,
        timeout_min=timeout_min,
        activity_id=activity_id,
        topic_id=topic_id,
        task_n=task_n,
        tmux_target_pane=tmux_target_pane,
        effort=effort,
    )


@mcp.tool()
def ow_close_worker(term_ref: str) -> dict:
    """アダプタ経由でworkerセッションをクローズする。

    OW_TERMINAL環境変数でアダプタを選択。アダプタ不在時はmanualフォールバック（手動クローズ案内）。

    Args:
        term_ref: 安定ID（iTerm2のsession UUID、tmuxのpane ID等。タブindexは不安定なため使用しない）

    Returns:
        成功時: {"closed": True, "term_ref": str}
        manualフォールバック時: {"manual": True, "message": str}
        失敗時: {"error": {...}}
    """
    return ow_service.ow_close_worker(term_ref)


@mcp.tool()
def ow_status(channel: str, topic_id: str | None = None) -> dict:
    """queueサマリ＋GetPresence（worker死活）の合成ビュー。

    queueの論理状態とrelayのpresence（物理接続）を統合して、orchが判断に使える単一ビューを返す。
    frontmatterフィールドにはqueueのYAMLフロントマター情報（channel_code, last_seen_msg_id等）が含まれる。

    Args:
        channel: channelコード（presence取得に使用）
        topic_id: queueファイル特定に使用（OW_QUEUE_DIRと組み合わせ）

    Returns:
        {
            "tasks": [...],
            "presence": [...],
            "frontmatter": {"topic_id": int, "orch_activity_id": int, "channel_code": str,
                           "orch_cwd": str, "last_seen_msg_id": int},
            "summary": {"total_tasks": int, "status_counts": dict, "online_workers": [...]}
        }
    """
    return ow_service.ow_status(channel, topic_id)


@mcp.tool()
def ow_recover(
    channel: str,
    topic_id: str,
    dry_run: bool = False,
    pending_spawn_stalled_threshold_min: int | None = None,
) -> dict:
    """orch crash後のqueue × relay履歴 × presence整合チェック・自動修正。

    relay履歴を since=0 で全件再走査して各worker毎の最新state宣言を集計し、queueと
    presenceに突合した上で次の4カテゴリに分類する。

    - ghost_active: queue=assigned/working なのに presence offline
      → relay 最新state宣言から queue ステータスを自動再構築（dry_run=Falseのみ）
    - pending_spawn: queue=spawning なのに presence offline
      → relay履歴に当該workerのstate宣言があれば自動更新、無く `pending_spawn_stalled_threshold_min`
        を超えた経過時間なら `auto_stalled` 化、それ未満なら起動進行中として放置
    - stalled_done: queue=done/closed/cancelled/failed なのに worker が presence onlineで残存
      → cmd:ping を送信して素性照会
    - orphans: presence online だが queue に登場しない w-* handle
      → cmd:ping で再リンク照会

    ping応答は orch の通常受信ループで処理する（本ツールは送信のみ）。queueの更新は
    ow_service内部の単一の接点関数経由のため、queue層の物理形が将来変わっても
    呼び出し側の影響範囲は最小。

    Args:
        channel: channelコード
        topic_id: トピックID（queue-t<topic_id>.md 特定に使用）
        dry_run: Trueなら検出のみ・修正/ping送信なし
        pending_spawn_stalled_threshold_min: pending_spawn (has_relay_history=False) を
            `auto_stalled` 化する経過時間閾値（分）。None なら従来挙動（自動更新対象外）。
            P0-7 で導入。「2日経っても failed 化されない pending_spawn」を orch が
            強制終端するために使う。

    Returns:
        成功時:
            {
                "detected": {"ghost_active": [...], "pending_spawn": [...], "stalled_done": [...], "orphans": [...]},
                "applied": {"queue_updates": [...], "pings_sent": [...]},
                "warnings": [str],
                "presence": [str],
                "reconstructed_max_msg_id": int,
                "dry_run": bool,
            }
        relay/channel不可時: {"error": {"code": ..., "message": ...}}
    """
    return ow_service.ow_recover(
        channel,
        topic_id,
        dry_run,
        pending_spawn_stalled_threshold_min=pending_spawn_stalled_threshold_min,
    )


# セッションエンドポイント（HTTPモード用カスタムルート）
@mcp.custom_route("/session/register", methods=["POST"])
async def session_register(request: Request) -> JSONResponse:
    """セッション登録エンドポイント"""
    mgr = get_session_manager()
    if mgr is None:
        return JSONResponse(
            {"error": "Session management not available (stdio mode)"},
            status_code=503,
        )
    try:
        body = await request.json()
        session_id = body.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return JSONResponse(
                {"error": "session_id is required (string)"},
                status_code=400,
            )
        is_new = mgr.register(session_id)
        return JSONResponse({
            "registered": is_new,
            "active_sessions": mgr.active_count,
        })
    except Exception as e:
        logger.exception("session_register failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/session/unregister", methods=["POST"])
async def session_unregister(request: Request) -> JSONResponse:
    """セッション解除エンドポイント"""
    mgr = get_session_manager()
    if mgr is None:
        return JSONResponse(
            {"error": "Session management not available (stdio mode)"},
            status_code=503,
        )
    try:
        body = await request.json()
        session_id = body.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return JSONResponse(
                {"error": "session_id is required (string)"},
                status_code=400,
            )
        removed = mgr.unregister(session_id)
        return JSONResponse({
            "unregistered": removed,
            "active_sessions": mgr.active_count,
        })
    except Exception as e:
        logger.exception("session_unregister failed")
        return JSONResponse({"error": str(e)}, status_code=500)


# サーバー起動
from src.http_config import HTTP_HOST, HTTP_PORT


if __name__ == "__main__":
    import argparse
    import os
    import signal

    parser = argparse.ArgumentParser(description="cc-memory MCP server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="トランスポート方式（デフォルト: stdio）",
    )
    args = parser.parse_args()

    from src.db import verify_sqlite_vec, init_database
    verify_sqlite_vec()
    init_database()

    if args.transport == "http":
        import socket
        from src.services.lock_file import acquire, release
        from src.services.session_manager import SessionManager

        # ポートの空き確認
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((HTTP_HOST, HTTP_PORT))
        except OSError:
            logger.error(f"Port {HTTP_PORT} is already in use")
            raise SystemExit(1)

        # ロックファイル取得
        if not acquire(HTTP_PORT):
            logger.error("Failed to acquire lock file. Another server may be running.")
            raise SystemExit(1)

        # セッションマネージャー初期化
        _session_manager = SessionManager()

        def _shutdown_server():
            """ウォッチドッグから呼ばれるシャットダウンハンドラ"""
            logger.info("Shutdown triggered by watchdog, sending SIGINT")
            os.kill(os.getpid(), signal.SIGINT)

        _session_manager.set_shutdown_callback(_shutdown_server)
        _session_manager.start_watchdog()

        try:
            logger.info(f"Starting HTTP server on {HTTP_HOST}:{HTTP_PORT}")
            mcp.run(transport="http", host=HTTP_HOST, port=HTTP_PORT)
        finally:
            release()
    else:
        mcp.run()
