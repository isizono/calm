"""招待URL redeem CLI: `python -m src.services.relay.redeem`。

標準入力から招待URLを1行受け取り、relay の `POST /invitations/redeem` へ
POST して bearer credential を取得し、`config.get_state_dir()/credential.json`
（0600、親 dir 0700）へ atomic に書き込む。

招待URLは argv ではなく標準入力で受ける（shell 履歴への招待URL残留を避けるため）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from src.services.relay import config

CREDENTIAL_FILENAME = "credential.json"

_REQUEST_TIMEOUT_SECONDS = 10.0


class RedeemError(RuntimeError):
    """redeem 処理が失敗したときに raise される。メッセージはそのまま利用者へ表示する。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_invite_url(url: str) -> tuple[str, str, str]:
    """招待URLを (redeem先エンドポイントURL, base_url, invite_token) に分解する。

    招待URLの形式: `<scheme>://<host>[:<port>]/invitations/redeem#v=1&t=it_...`
    - scheme+host+path が POST 先エンドポイント
    - scheme+host が credential.json に保存する base_url
    - fragment の `t=` が invite token
    """
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc or not parsed.path:
        raise RedeemError(f"招待URLの形式が不正です（scheme/host/pathが揃っていません）: {url!r}")
    if not parsed.fragment:
        raise RedeemError("招待URLに fragment（#v=1&t=...）がありません")

    fragment_params = dict(parse_qsl(parsed.fragment))
    token = fragment_params.get("t")
    if not token:
        raise RedeemError("招待URLの fragment に招待token（t=...）がありません")

    base_url = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = f"{base_url}{parsed.path}"
    return endpoint, base_url, token


def _redeem(endpoint: str, invite_token: str) -> dict:
    """redeem endpoint へ POST し、成功時の応答 body（dict）を返す。

    HTTP 応答に至らない transport error（connection refused 等）は非200分岐と
    区別し、relay の稼働確認を促すメッセージで RedeemError を送出する。
    """
    try:
        response = httpx.post(
            endpoint,
            json={"invite_token": invite_token},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        raise RedeemError(
            "relay に接続できませんでした。relay サーバーが起動しているか確認してください"
            "（例: launchctl print gui/$(id -u)/com.isizono.relay-v2）。"
            f" 詳細: {exc}"
        ) from exc

    if response.status_code != 200:
        code = "?"
        message = response.text
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            code = body.get("code", code)
            message = body.get("message", message)
        raise RedeemError(f"redeem に失敗しました（HTTP {response.status_code} {code}）: {message}")

    try:
        body = response.json()
    except ValueError as exc:
        raise RedeemError("redeem 応答の JSON parse に失敗しました") from exc
    if not isinstance(body, dict) or not body.get("bearer_token") or not body.get("identity"):
        raise RedeemError(f"redeem 応答に bearer_token / identity がありません: {body!r}")
    return body


def _write_credential_file(state_dir: Path, credential: dict) -> None:
    """credential.json を temp file → atomic rename で 0600 書込する。

    temp file は最終 dir と同一 FS 上（`tempfile.mkstemp(dir=state_dir)`）に
    作る。cross-FS だと rename が非 atomic な copy に fallback し、平文 bearer
    が一時的に広い権限で露出し得るため。
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)

    fd, tmp_path_str = tempfile.mkstemp(dir=str(state_dir), prefix=".credential-", suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(credential, f)
        os.replace(tmp_path, state_dir / CREDENTIAL_FILENAME)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _report_write_failure(state_dir: Path, credential: dict, exc: OSError) -> None:
    """credential ファイル書込失敗時、bearer を stderr に出して手動回復を促す。

    invite は既に消費済み・bearer は relay 側に mint 済みだが client に届かない
    孤児 credential が生じる（E4）。手動でファイルへ貼るか、revoke + 再発行で
    孤児を無効化することを促す。
    """
    print(f"credential ファイルの書き込みに失敗しました: {exc}", file=sys.stderr)
    print(
        f"手動回復: 以下の bearer_token を {state_dir / CREDENTIAL_FILENAME} に"
        "手貼りするか、"
        f"`python -m relay.invite revoke --identity {credential.get('identity')}` の後に"
        "再発行してください。",
        file=sys.stderr,
    )
    print(f"bearer_token: {credential.get('bearer_token')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    url = sys.stdin.readline().strip()
    if not url:
        print("標準入力から招待URLを受け取れませんでした。", file=sys.stderr)
        return 1

    try:
        endpoint, base_url, invite_token = _parse_invite_url(url)
    except RedeemError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        body = _redeem(endpoint, invite_token)
    except RedeemError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    credential = {
        "base_url": base_url,
        "identity": body["identity"],
        "bearer_token": body["bearer_token"],
        "issued_at": _now_iso(),
        "expires_at": body.get("expires_at"),
    }

    state_dir = config.get_state_dir()
    try:
        _write_credential_file(state_dir, credential)
    except OSError as exc:
        _report_write_failure(state_dir, credential, exc)
        return 1

    print(f"credential を取得しました: identity={credential['identity']} expires_at={credential['expires_at']}")
    print(f"保存先: {state_dir / CREDENTIAL_FILENAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
