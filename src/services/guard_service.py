"""worker セッション向けの構造的ガード。

ow worker として起動されたセッションが、ユーザー合意を必要とする記録系ツール
(add_decisions / add_topic / add_habit) を直接呼び出すのを構造的に阻止する。
worker は task に集中するため、これらの記録はユーザー合意に基づいて
orch 経由で行う。判断を orch に仰ぐためのエスカレーション通路では
OW_ESCALATION=1 を立てて通過させる。

note: add_logs は worker-sync の退場処理で必須呼び出しなのでガード対象外。
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
    "ユーザー合意に基づいて orch 経由で記録してください "
    "(orch_proxy 経路では OW_ESCALATION=1 で通過します)。"
)


def check_worker_guard(tool_name: str) -> None:
    """worker セッションかつ非エスカレーション時に WorkerGuardError を raise する。

    add_decisions / add_topic / add_habit 等、worker が直接呼んではならない
    ツールの冒頭で呼ぶ。OW_ESCALATION=1 のときは通過する。
    """
    if is_worker_session() and not is_escalation_mode():
        raise WorkerGuardError(_WORKER_GUARD_MESSAGE_TMPL.format(tool_name=tool_name))
