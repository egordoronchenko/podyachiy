"""Расходы и журнал действий."""
from __future__ import annotations

import asyncio
import datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import costbook, journal, users
from .access import admin_guard, costs_guard, viewer
from .browse import _dir
from .core import people, trail

router = APIRouter()


def _day(value: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat() if value else ""
    except ValueError:
        raise HTTPException(400, "дата — в виде ГГГГ-ММ-ДД")


@router.get("/api/v1/costs", dependencies=[Depends(costs_guard)])
@router.get("/api/admin/report", dependencies=[Depends(costs_guard)])
async def admin_report(start: str = "", end: str = "", bucket: str = "day"):
    """Расходы за период: по папкам, людям, записям, этапам, поставщикам и по времени.
    Каждая сумма — по валютам, как её вернул поставщик: рубли и доллары не пересчитываются
    и не складываются. Второй адрес — тот же отчёт для внешних систем. Оба — только
    администратору сервиса: самому или его ключу «с управлением»."""
    if bucket not in journal.BUCKETS:
        raise HTTPException(400, "bucket: " + " | ".join(journal.BUCKETS))
    return {**await asyncio.to_thread(trail.report, _day(start), _day(end), bucket),
            "names": {u["login"]: u["name"] for u in people.public()}}


async def _costbook(start: str, end: str, bucket: str, who: users.Access) -> tuple[list, str, str]:
    """Отчёт о расходах для файла — весь, по выбранным периоду и шагу; с ним границы периода для имени файла."""
    if bucket not in journal.BUCKETS:
        raise HTTPException(400, "bucket: " + " | ".join(journal.BUCKETS))
    start, end = _day(start), _day(end)
    runs = await asyncio.to_thread(trail.within, start, end)
    book = costbook.sheets(journal.summary(runs, start, end, bucket), runs,
                           {u["login"]: u["name"] for u in people.public()},
                           datetime.datetime.now().astimezone().replace(microsecond=0), who.login)
    return book, start, end


@router.get("/api/admin/report.xlsx", dependencies=[Depends(costs_guard)])
async def admin_report_xlsx(start: str = "", end: str = "", bucket: str = "day", who: users.Access = Depends(viewer)):
    """Отчёт о расходах книгой Excel — целиком: сводка, по периодам, папкам, людям, записям,
    этапам, поставщикам и журнал прогонов, каждый разрез на своём листе."""
    book, start, end = await _costbook(start, end, bucket, who)
    return Response(await asyncio.to_thread(costbook.to_xlsx, book), media_type=costbook.XLSX, headers={
        "Content-Disposition": f'attachment; filename="{costbook.name(start, end, "xlsx")}"'})


@router.get("/api/admin/report.csv", dependencies=[Depends(costs_guard)])
async def admin_report_csv(start: str = "", end: str = "", bucket: str = "day", who: users.Access = Depends(viewer)):
    """Тот же отчёт одним файлом CSV: те же таблицы подряд, у каждой название и шапка."""
    book, start, end = await _costbook(start, end, bucket, who)
    return Response(costbook.to_csv(book), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{costbook.name(start, end, "csv")}"'})


@router.get("/api/admin/journal", dependencies=[Depends(admin_guard)])
async def admin_journal(limit: int = 200, kind: str = "", by: str = "", file: str = "", dir: str = "",
                        login: str = "", since: str = "", until: str = ""):
    """Последние действия людей: входы, загрузки, папки, права, пользователи. Отбор — по виду
    события (kind), по тому, кто сделал (by), по записи (file), папке с подпапками (dir),
    по пользователю, о котором событие (login), и по дням (since, until)."""
    return {"events": await asyncio.to_thread(trail.actions, max(1, min(limit, 2000)), kind, by, file,
                                              _dir(dir), login, _day(since), _day(until))}
