"""Background search task manager — runs queries in threads."""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from database import DedupDB
from search import MapsClient, Business, build_fingerprint

logger = logging.getLogger(__name__)

# Global rate limiter — shared across all tasks
_lock = threading.Lock()
_last_req = 0.0


@dataclass
class Task:
    id: str
    queries: list[str]
    limit: int
    enrich: bool
    recheck_days: int
    status: str = "pending"       # pending | running | done | error | cancelled
    progress: str = ""
    leads: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    _stop: bool = field(default=False, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "queries": self.queries,
            "limit": self.limit,
            "status": self.status,
            "progress": self.progress,
            "total_found": self.stats.get("found", 0),
            "new_leads": self.stats.get("new", 0),
            "already_seen": self.stats.get("seen", 0),
            "with_site": self.stats.get("with_site", 0),
            "no_site": self.stats.get("no_site", 0),
            "unknown_site": self.stats.get("unknown_site", 0),
            "leads_count": len(self.leads),
            "error": self.error,
            "created_at": self.created_at,
        }


class TaskManager:
    def __init__(self, api_key: str, db_path: str = "data/dedup.db"):
        self.client = MapsClient(api_key)
        self.db = DedupDB(db_path)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, queries: list[str], limit: int = 20,
               enrich: bool = False, recheck_days: int = 0) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            queries=queries,
            limit=limit,
            enrich=enrich,
            recheck_days=recheck_days,
        )
        with self._lock:
            self._tasks[task.id] = task
        t = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread = t
        t.start()
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task.status in ("pending", "running"):
            task._stop = True
            task.status = "cancelled"
            return True
        return False

    def list_tasks(self) -> list[dict]:
        with self._lock:
            return sorted(
                [t.to_dict() for t in self._tasks.values()],
                key=lambda x: x["created_at"],
                reverse=True,
            )

    # ── Background execution ───────────────────────────────────────

    def _run(self, task: Task) -> None:
        task.status = "running"
        totals = {"found": 0, "new": 0, "seen": 0,
                  "with_site": 0, "no_site": 0, "unknown_site": 0}
        try:
            for qi, query in enumerate(task.queries, 1):
                if task._stop:
                    break
                task.progress = f"[{qi}/{len(task.queries)}] {query}"
                leads, st = self._search_one(task, query)
                task.leads.extend(leads)
                for k in totals:
                    totals[k] += st.get(k, 0)
                task.stats = dict(totals)
        except Exception as exc:
            task.status = "error"
            task.error = str(exc)
            logger.exception("Task %s failed", task.id)
            return

        task.status = "done"
        task.progress = "Concluído"
        task.stats = dict(totals)

    def _search_one(self, task: Task, query: str) -> tuple[list[dict], dict]:
        stats = {"found": 0, "new": 0, "seen": 0,
                 "with_site": 0, "no_site": 0, "unknown_site": 0}
        results: list[dict] = []
        token = None

        for page in range(3):
            if task._stop:
                break
            data = self.client.search(query, page_token=token)
            places = data.get("places", [])
            stats["found"] += len(places)
            if not places:
                break

            for raw in places:
                if task._stop:
                    break
                pid = raw.get("id", "")
                biz = Business.__init__ is not None  # placeholder
                from search import to_business
                biz = to_business(raw, query)
                fp = build_fingerprint(biz.name, biz.address, biz.phone)

                if self.db.is_seen(pid, fp):
                    if task.recheck_days and self.db.needs_recheck(pid, fp, task.recheck_days):
                        pass  # revalidate — continue
                    else:
                        stats["seen"] += 1
                        continue

                # Optionally enrich with phone/details
                if task.enrich and not biz.phone:
                    d = self.client.details(pid)
                    if d:
                        biz.phone = d.get("internationalPhoneNumber", "") or d.get("nationalPhoneNumber", "")
                        oh = d.get("regularOpeningHours")
                        if oh and isinstance(oh, dict):
                            biz.hours = "; ".join(oh.get("weekdayDescriptions", [])[:7])

                # Classify website
                hw = biz.has_website
                if hw == "true":
                    stats["with_site"] += 1
                elif hw == "false":
                    stats["no_site"] += 1
                else:
                    stats["unknown_site"] += 1

                self.db.record(pid, fp, hw)
                stats["new"] += 1
                results.append(_biz_to_dict(biz))

            token = data.get("nextPageToken")
            if not token:
                break

        return results, stats


def _biz_to_dict(b: Business) -> dict:
    return {
        "name": b.name,
        "address": b.address,
        "city": b.city,
        "state": b.state,
        "country": b.country,
        "phone": b.phone,
        "website": b.website,
        "maps_url": b.maps_url,
        "place_id": b.place_id,
        "latitude": b.latitude,
        "longitude": b.longitude,
        "rating": b.rating,
        "reviews": b.reviews,
        "hours": b.hours,
        "categories": b.categories,
        "has_website": b.has_website,
        "source_query": b.source_query,
        "collected_at": b.collected_at,
    }
