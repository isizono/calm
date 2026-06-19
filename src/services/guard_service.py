"""worker セッション向けの構造的ガード。

ow worker として起動されたセッションが、ユーザー合意を必要とする記録系ツール
(add_decisions / add_logs / add_topic) を直接呼び出すのを構造的に阻止する。
worker は task に集中するため、自由な記録はせずに以下のいずれかを経由する:

- recording skill 経由でユーザー合意プロセスを伴って記録する
- 判断を仰ぐ場合は orch にエスカレーションし、orch が代行する
  (orch_proxy 経路では OW_ESCALATION=1 を立てて通過させる)
"""
import os


class WorkerGuardError(RuntimeError):
    """worker セッションが直接呼び出せないツールを呼んだときに raise される。"""


_ROLE_ENV = "OW_ROLE"
_ROLE_WORKER = "worker"
_ESCALATION_ENV = "OW_ESCALATION"
_ESCALATION_PASS = "1"


def is_worker_session() -> bool:
    """ow worker として起動されたセッションかを判定する。

    ow_spawn_worker は worker 起動時に環境変数 OW_ROLE=worker を設定する。
    エスカレーション状態 (OW_ESCALATION) はここでは見ない。
    """
    return os.environ.get(_ROLE_ENV) == _ROLE_WORKER


def is_escalation_mode() -> bool:
    """orch_proxy 経由でエスカレーション通路に乗っているかを判定する。"""
    return os.environ.get(_ESCALATION_ENV) == _ESCALATION_PASS


_WORKER_GUARD_MESSAGE_TMPL = (
    "{tool_name} は worker セッションから直接呼び出せません。"
    "ユーザー合意プロセスを伴う記録は recording skill 経由で行うか、"
    "判断を仰ぐ場合は orch にエスカレーションしてください "
    "(orch_proxy 経路では OW_ESCALATION=1 で通過します)。"
)


def check_worker_guard(tool_name: str) -> None:
    """worker セッションかつ非エスカレーション時に WorkerGuardError を raise する。

    add_decisions / add_logs / add_topic 等、worker が直接呼んではならない
    ツールの冒頭で呼ぶ。OW_ESCALATION=1 のときは通過する。
    """
    if is_worker_session() and not is_escalation_mode():
        raise WorkerGuardError(_WORKER_GUARD_MESSAGE_TMPL.format(tool_name=tool_name))
