from __future__ import annotations

import re

from harnesslab.agentic.contracts import TaskAssessment, TaskClass, TaskSignal


class TaskRouter:
    """Explainable task-complexity router used before spending a model turn."""

    _MULTI_STEP = re.compile(
        r"\b(and then|after that|before|finally|first|second|then)\b|"
        r"然后|接着|最后|先.+再|第一.+第二",
        re.IGNORECASE,
    )
    _SIDE_EFFECT = re.compile(
        r"\b(deploy|delete|write|update|send|purchase|publish|merge|release)\b|"
        r"部署|删除|写入|更新|发送|购买|发布|合并|上线",
        re.IGNORECASE,
    )
    _RESEARCH = re.compile(
        r"\b(compare|research|investigate|evaluate|root cause|trade-?off)\b|"
        r"比较|调研|排查|评估|根因|权衡",
        re.IGNORECASE,
    )
    _DEPENDENCY = re.compile(
        r"\b(if|unless|depends on|rollback|fallback|retry|parallel)\b|"
        r"如果|依赖|回滚|降级|重试|并行",
        re.IGNORECASE,
    )
    _HIGH_RISK = re.compile(
        r"\b(prod(?:uction)?|payment|credential|permission|security|database)\b|"
        r"生产|支付|凭证|权限|安全|数据库",
        re.IGNORECASE,
    )

    def assess(self, task: str) -> TaskAssessment:
        normalized = " ".join(task.split())
        signals: list[TaskSignal] = []
        self._add_signal(signals, self._MULTI_STEP, normalized, "multi-step-language", 2)
        self._add_signal(signals, self._SIDE_EFFECT, normalized, "side-effect", 3)
        self._add_signal(signals, self._RESEARCH, normalized, "open-ended-reasoning", 2)
        self._add_signal(signals, self._DEPENDENCY, normalized, "control-flow", 2)
        self._add_signal(signals, self._HIGH_RISK, normalized, "high-risk-domain", 3)

        clauses = len([part for part in re.split(r"[，。；,;.!?！？]", normalized) if part])
        if clauses >= 4:
            signals.append(TaskSignal(name="many-clauses", weight=2, evidence=f"{clauses} clauses"))
        if len(normalized) >= 240:
            signals.append(
                TaskSignal(name="long-request", weight=1, evidence=f"{len(normalized)} characters")
            )

        score = sum(signal.weight for signal in signals)
        task_class = TaskClass.COMPLEX if score >= 4 else TaskClass.SIMPLE
        parallelism = 1 if task_class == TaskClass.SIMPLE else min(4, 2 + score // 5)
        return TaskAssessment(
            task_class=task_class,
            score=score,
            signals=signals,
            recommended_parallelism=parallelism,
            requires_planning=task_class == TaskClass.COMPLEX,
        )

    @staticmethod
    def _add_signal(
        signals: list[TaskSignal],
        pattern: re.Pattern[str],
        task: str,
        name: str,
        weight: int,
    ) -> None:
        if match := pattern.search(task):
            signals.append(TaskSignal(name=name, weight=weight, evidence=match.group(0)))
