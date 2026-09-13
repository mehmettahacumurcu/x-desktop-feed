from collections.abc import Sequence
import math

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QLabel

from xfeed.collection import (
    CandidateObservation,
    DiscoveryReason,
    DiscoveryResult,
    qualifying_candidates,
    qualifying_home_candidates,
)
from xfeed.domain import CollectionTarget, CollectionTargetKind
from xfeed.i18n import tr
from xfeed.opera_bridge import OperaBridge
from xfeed.opera_protocol import OperaJobResult


class OperaExtensionCollector(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(
        self,
        bridge: OperaBridge,
        *,
        timeout_s: float = 45.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")

        self.widget = QLabel(tr("Collection is running in the dedicated Opera GX tab."))
        self.widget.setWordWrap(True)
        self._bridge = bridge
        self._maximum = 30
        self._timeout_s = float(timeout_s)
        self._generation = 0
        self._active_generation: int | None = None
        self._active_job_id: str | None = None
        self._target: CollectionTarget | None = None
        self._candidates: list[CandidateObservation] = []
        self._seen_post_ids: set[str] = set()
        self._bridge.candidate_received.connect(self._on_candidate_received)
        self._bridge.job_finished.connect(self._on_job_finished)

    def start(self, target: CollectionTarget, maximum: int = 30) -> None:
        upper = 30 if target.kind is CollectionTargetKind.SOURCE else 50
        if type(maximum) is not int or not 1 <= maximum <= upper:
            raise ValueError(f"maximum must be between 1 and {upper}")
        self._cancel_active(emit_result=False)
        self._generation += 1
        generation = self._generation
        self._active_generation = generation
        self._active_job_id = None
        self._target = target
        self._maximum = maximum
        self._candidates = []
        self._seen_post_ids = set()

        try:
            job = self._bridge.enqueue(target, maximum, self._timeout_s)
        except (TypeError, ValueError, RuntimeError) as error:
            self._finish(generation, DiscoveryReason.ERROR, str(error))
            return

        if generation != self._active_generation:
            self._bridge.cancel(job.id)
            return
        self._active_job_id = job.id

    def cancel(self) -> None:
        self._cancel_active(emit_result=True)

    def _cancel_active(self, *, emit_result: bool) -> None:
        generation = self._active_generation
        job_id = self._active_job_id
        if generation is None:
            return

        self._active_generation = None
        self._active_job_id = None
        if job_id is not None:
            try:
                self._bridge.cancel(job_id)
            except RuntimeError:
                pass
        if emit_result:
            self.finished.emit(DiscoveryResult(tuple(self._candidates), DiscoveryReason.CANCELLED))

    def _on_candidate_received(self, job_id: object, observation: object) -> None:
        if (
            self._active_generation is None
            or job_id != self._active_job_id
            or not isinstance(observation, CandidateObservation)
            or self._target is None
            or len(self._candidates) >= self._maximum
            or observation.post_id in self._seen_post_ids
        ):
            return

        target = self._target
        qualified: Sequence[CandidateObservation]
        if target.kind is CollectionTargetKind.SOURCE:
            assert target.handle is not None
            qualified = qualifying_candidates((observation,), target.handle)
        else:
            qualified = qualifying_home_candidates((observation,), maximum=self._maximum)
        if not qualified:
            return
        candidate = qualified[0]
        self._seen_post_ids.add(candidate.post_id)
        self._candidates.append(candidate)
        self.progress.emit(candidate)

    def _on_job_finished(self, result: object) -> None:
        if (
            not isinstance(result, OperaJobResult)
            or self._active_generation is None
            or result.job_id != self._active_job_id
        ):
            return
        self._finish(self._active_generation, result.reason, result.diagnostic)

    def _finish(
        self,
        generation: int,
        reason: DiscoveryReason,
        diagnostic: str | None = None,
    ) -> None:
        if generation != self._active_generation:
            return
        self._active_generation = None
        self._active_job_id = None
        self.finished.emit(DiscoveryResult(tuple(self._candidates), reason, diagnostic))
