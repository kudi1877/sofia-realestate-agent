"""Weekly watcher for Sofia municipal and SOAPI property-sale notices.

Recon on 2026-07-12 found two official server-rendered Liferay indexes:

* ``https://www.sofia.bg/bg/tenders`` exposes 10 notices per page. Three pages
  cover roughly a month of weekly-run downtime without crawling the archive.
* ``https://council.sofia.bg/decisions-soapi`` currently exposes the latest
  three SOAPI decisions on one page with no paginator.

Both sites returned ``403 Forbidden: Unsupported Browser`` to bounded direct
``httpx``/curl checks in the development environment, while a real browser
rendered them normally. Network failures therefore leave the notice ledger and
existing auctions untouched. Detail pages are HTML-first; when they link a PDF,
pdfminer text is preferred and the HTML body is the fallback.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Protocol
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from src.utils.soup import make_soup
from loguru import logger
from pdfminer.high_level import extract_text as pdf_extract_text
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from src.config import (
    EUR_BGN_RATE,
    LLM_PROVIDER,
    MUNICIPAL_DELAY_SECONDS,
    MUNICIPAL_MAX_NOTICES_PER_RUN,
    MUNICIPAL_MAX_PAGES,
    MUNICIPAL_WATCH_WEEKDAY,
    SOFIA_NEIGHBORHOODS,
)
from src.database.models import SeenNotice
from src.database.repository import ListingRepository
from src.enrichment.llm_extract import (
    AnthropicProvider,
    ExtractionProvider,
    LocalProvider,
    MoonshotProvider,
    ProviderResult,
    build_provider,
)
from src.utils.neighborhoods import canonicalize_neighborhood
from src.utils.time import utc_now


SOFIA_TENDERS_URL = "https://www.sofia.bg/bg/tenders"
SOAPI_URL = "https://council.sofia.bg/decisions-soapi"
RESIDENTIAL_RE = re.compile(r"\b(?:апартамент|жилище|имот)\b", re.IGNORECASE)
SALE_RE = re.compile(r"\b(?:продажба|продава|публичен\s+търг|търг\s+с)\b", re.IGNORECASE)
RENT_RE = re.compile(r"отдаване\s+под\s+наем|наемна\s+цена", re.IGNORECASE)
_KNOWN_HOODS = sorted(SOFIA_NEIGHBORHOODS, key=len, reverse=True)


@dataclass(frozen=True)
class MunicipalNotice:
    source: str
    source_id: str
    title: str
    notice_date: datetime | None
    url: str


class MunicipalExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_type: Literal["apartment", "studio", "maisonette", "house", "villa", "plot", "other"]
    address: str | None = Field(default=None, max_length=500)
    area_sqm: float | None = Field(default=None, gt=0, le=100_000)
    starting_price: float | None = Field(default=None, gt=0)
    currency: Literal["EUR", "BGN"] | None = None
    deadline: str | None = Field(default=None, max_length=100)


class MunicipalProvider(Protocol):
    name: str

    def extract_notice(self, text: str) -> ProviderResult: ...


MUNICIPAL_SYSTEM_PROMPT = """Extract a Sofia municipal property-sale notice.
Return only explicitly stated values. starting_price is the auction's opening
sale price, never a deposit, bid step, monthly rent, or document fee. Preserve
the stated EUR/BGN currency. deadline is the auction date or final bid deadline
in ISO YYYY-MM-DD form when stated. Use null when evidence is absent."""
MUNICIPAL_TOOL = "record_municipal_property_sale"


class AnthropicMunicipalProvider:
    name = "anthropic"

    def __init__(self, base: AnthropicProvider):
        self.base = base

    def extract_notice(self, text: str) -> ProviderResult:
        response = self.base.client.messages.create(
            model=self.base.model,
            max_tokens=500,
            temperature=0,
            system=MUNICIPAL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text[:16_000]}],
            tools=[
                {
                    "name": MUNICIPAL_TOOL,
                    "description": "Record one municipal property sale as structured JSON.",
                    "input_schema": MunicipalExtraction.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": MUNICIPAL_TOOL},
        )
        block = next((item for item in response.content if getattr(item, "type", None) == "tool_use"), None)
        if block is None:
            raise ValueError("Anthropic municipal response had no tool result")
        return ProviderResult(data=dict(block.input), model=response.model, cost_usd=0.0)


class LocalMunicipalProvider:
    name = "local"

    def __init__(self, base: LocalProvider):
        self.base = base

    def extract_notice(self, text: str) -> ProviderResult:
        response = self.base.client.post(
            self.base.url,
            json={
                "model": self.base.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": MUNICIPAL_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Schema:\n{json.dumps(MunicipalExtraction.model_json_schema())}\n\n"
                            f"Notice:\n{text[:16_000]}"
                        ),
                    },
                ],
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        return ProviderResult(data=json.loads(content), model=self.base.model, cost_usd=0.0)


def build_municipal_provider(name: str = LLM_PROVIDER) -> MunicipalProvider | None:
    provider: ExtractionProvider | None = build_provider(name)
    if provider is None:
        return None
    if isinstance(provider, AnthropicProvider):
        return AnthropicMunicipalProvider(provider)
    # Moonshot (Kimi) speaks the same OpenAI-compatible chat API as the local
    # provider — same .client/.url/.model — so it reuses that path. Switching
    # LLM_PROVIDER=moonshot otherwise crashed the whole nightly here (2026-07-20).
    if isinstance(provider, (LocalProvider, MoonshotProvider)):
        return LocalMunicipalProvider(provider)
    logger.warning(
        f"Municipal notices skipped: no adapter for {type(provider).__name__}"
    )
    return None


def is_watch_day(now: datetime | None = None, weekday: int = MUNICIPAL_WATCH_WEEKDAY) -> bool:
    return (now or utc_now()).weekday() == weekday


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _source_id(source: str, url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    if re.fullmatch(r"[\w-]{1,80}", tail):
        return tail
    return hashlib.sha256(f"{source}:{_clean_url(url)}".encode()).hexdigest()[:24]


def _parse_notice_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip().casefold())
    months = {
        "януари": 1, "февруари": 2, "март": 3, "април": 4,
        "май": 5, "юни": 6, "юли": 7, "август": 8,
        "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
    }
    named = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", normalized)
    if named and named.group(2) in months:
        return datetime(int(named.group(3)), months[named.group(2)], int(named.group(1)), 23, 59, 59)
    for pattern in (r"(\d{4})-(\d{2})-(\d{2})", r"(\d{1,2})[./](\d{1,2})[./](\d{4})"):
        match = re.search(pattern, normalized)
        if not match:
            continue
        try:
            if pattern.startswith(r"(\d{4})"):
                year, month, day = map(int, match.groups())
            else:
                day, month, year = map(int, match.groups())
            return datetime(year, month, day, 23, 59, 59)
        except ValueError:
            return None
    return None


def _is_sale_candidate(text: str, source: str) -> bool:
    if RENT_RE.search(text):
        return False
    return bool(RESIDENTIAL_RE.search(text) and (SALE_RE.search(text) or source == "soapi"))


def _neighborhood(*values: str | None) -> str:
    text = " ".join(value or "" for value in values).casefold()
    for hood in _KNOWN_HOODS:
        if re.search(rf"(?:^|\W){re.escape(hood.casefold())}(?:\W|$)", text):
            return canonicalize_neighborhood(hood)
    return "Unknown"


class MunicipalNoticeWatcher:
    def __init__(
        self,
        *,
        max_pages: int = MUNICIPAL_MAX_PAGES,
        max_notices: int = MUNICIPAL_MAX_NOTICES_PER_RUN,
        delay_seconds: float = MUNICIPAL_DELAY_SECONDS,
        client: httpx.Client | None = None,
    ):
        self.max_pages = max(1, max_pages)
        self.max_notices = max(1, max_notices)
        self.delay_seconds = max(0.0, delay_seconds)
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
                "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.7",
            },
        )
        self._requests = 0
        self.source_errors: List[str] = []

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _get(self, url: str) -> httpx.Response:
        if self._requests and self.delay_seconds:
            time.sleep(self.delay_seconds)
        response = self.client.get(url)
        self._requests += 1
        response.raise_for_status()
        return response

    @staticmethod
    def parse_index(soup: BeautifulSoup, source: str) -> List[MunicipalNotice]:
        notices: List[MunicipalNotice] = []
        if source == "sofia_tenders":
            links = soup.select('a[href*="/w/"]')
            base_url = SOFIA_TENDERS_URL
        elif source == "soapi":
            links = soup.select('a[href*="/decisions-soapi/-/asset_publisher/"][href*="/content/"]')
            base_url = SOAPI_URL
        else:
            raise ValueError(f"Unknown municipal source: {source}")

        seen_urls = set()
        for link in links:
            url = _clean_url(urljoin(base_url, link.get("href", "")))
            if not url or url in seen_urls:
                continue
            if source == "sofia_tenders":
                container = link.find_parent(
                    lambda tag: tag.name in {"article", "div", "li"}
                    and tag.find(["h3", "h4"])
                    and tag.find("h5")
                )
                title_node = container.find("h4") if container else link.find_previous("h4")
                date_node = container.find("h5") if container else link.find_next("h5")
                title = title_node.get_text(" ", strip=True) if title_node else ""
                date_text = date_node.get_text(" ", strip=True) if date_node else ""
            else:
                title = link.get_text(" ", strip=True)
                date_text = title
            if not title:
                continue
            seen_urls.add(url)
            notices.append(
                MunicipalNotice(
                    source=source,
                    source_id=_source_id(source, url),
                    title=title,
                    notice_date=_parse_notice_date(date_text),
                    url=url,
                )
            )
        return notices

    def _sofia_page_url(self, page: int) -> str:
        if page == 1:
            return SOFIA_TENDERS_URL
        params = {
            "p_p_id": "com_liferay_asset_publisher_web_portlet_AssetPublisherPortlet_INSTANCE_6KiADf1joLKS",
            "p_p_lifecycle": "0",
            "p_p_state": "normal",
            "p_p_mode": "view",
            "_com_liferay_asset_publisher_web_portlet_AssetPublisherPortlet_INSTANCE_6KiADf1joLKS_redirect": "/bg/tenders",
            "_com_liferay_asset_publisher_web_portlet_AssetPublisherPortlet_INSTANCE_6KiADf1joLKS_delta": "10",
            "p_r_p_resetCur": "false",
            "_com_liferay_asset_publisher_web_portlet_AssetPublisherPortlet_INSTANCE_6KiADf1joLKS_cur": str(page),
        }
        return f"{SOFIA_TENDERS_URL}?{urlencode(params)}"

    def fetch_notices(self) -> List[MunicipalNotice]:
        notices: List[MunicipalNotice] = []
        for page in range(1, self.max_pages + 1):
            try:
                response = self._get(self._sofia_page_url(page))
            except Exception as exc:
                message = f"sofia_tenders page {page}: {exc}"
                logger.warning(f"Municipal watcher could not fetch {message}")
                self.source_errors.append(message)
                break
            parsed = self.parse_index(make_soup(response.text), "sofia_tenders")
            notices.extend(parsed)
            if not parsed:
                break

        try:
            response = self._get(SOAPI_URL)
            notices.extend(self.parse_index(make_soup(response.text), "soapi"))
        except Exception as exc:
            message = f"soapi: {exc}"
            logger.warning(f"Municipal watcher could not fetch {message}")
            self.source_errors.append(message)

        unique = {(notice.source, notice.source_id): notice for notice in notices}
        return list(unique.values())

    def fetch_notice_text(self, notice: SeenNotice) -> tuple[str | None, str | None]:
        try:
            response = self._get(notice.url)
        except Exception as exc:
            logger.warning(f"Municipal detail fetch failed for {notice.url}: {exc}")
            return None, None
        soup = make_soup(response.text)
        candidates = soup.select(".asset-full-content, .asset-content, .journal-content-article, main")
        # SOAPI renders a shared 650-character agency header before the actual
        # 1-2k decision article. Longest content reliably selects the decision.
        main = max(candidates, key=lambda node: len(node.get_text(" ", strip=True))) if candidates else soup
        html_text = main.get_text(" ", strip=True)
        pdf_link = next(
            (
                link.get("href")
                for link in main.select("a[href]")
                if ".pdf" in link.get("href", "").casefold()
                or "/documents/" in link.get("href", "").casefold()
            ),
            None,
        )
        if not pdf_link:
            return html_text, None
        pdf_url = _clean_url(urljoin(notice.url, pdf_link))
        try:
            pdf_response = self._get(pdf_url)
            if len(pdf_response.content) > 12 * 1024 * 1024:
                raise ValueError("PDF exceeds 12 MB cap")
            pdf_text = pdf_extract_text(io.BytesIO(pdf_response.content)).strip()
            return (pdf_text or html_text), pdf_url
        except Exception as exc:
            logger.warning(f"Municipal PDF extraction failed for {pdf_url}: {exc}; using HTML")
            return html_text, pdf_url

    def run(
        self,
        db: Session,
        *,
        provider: MunicipalProvider | None = None,
        provider_name: str = LLM_PROVIDER,
        force: bool = False,
        now: datetime | None = None,
    ) -> Dict[str, Any]:
        run_at = now or utc_now()
        if not force and not is_watch_day(run_at):
            return {
                "skipped": "weekday_gate",
                "new_notices": 0,
                "candidates": 0,
                "listings_created": 0,
                "pending": 0,
                "errors": [],
            }

        discovered = self.fetch_notices()
        new_notices = 0
        for notice in discovered:
            existing = db.query(SeenNotice).filter_by(source=notice.source, source_id=notice.source_id).first()
            if existing:
                continue
            candidate = notice.source == "soapi" or _is_sale_candidate(notice.title, notice.source)
            row = SeenNotice(
                source=notice.source,
                source_id=notice.source_id,
                title=notice.title,
                notice_date=notice.notice_date,
                url=notice.url,
                is_residential=candidate,
                processed_at=None if candidate else run_at,
            )
            db.add(row)
            new_notices += 1
        db.commit()

        if provider is None:
            provider = build_municipal_provider(provider_name)

        pending_rows = (
            db.query(SeenNotice)
            .filter(SeenNotice.is_residential.is_(True), SeenNotice.processed_at.is_(None))
            .order_by(SeenNotice.notice_date.desc(), SeenNotice.id.desc())
            .limit(self.max_notices)
            .all()
        )
        created = processed = failed = 0
        repo = ListingRepository(db)
        for notice in pending_rows:
            text, pdf_url = self.fetch_notice_text(notice)
            if pdf_url:
                notice.pdf_url = pdf_url
            if not text:
                notice.last_error = "detail_fetch_failed"
                failed += 1
                db.commit()
                continue
            combined = f"{notice.title}\n{text}"
            if not _is_sale_candidate(combined, notice.source):
                notice.is_residential = False
                notice.processed_at = run_at
                notice.last_error = None
                processed += 1
                db.commit()
                continue
            if provider is None:
                notice.last_error = "provider_off"
                db.commit()
                continue
            try:
                result = provider.extract_notice(combined)
                extraction = MunicipalExtraction.model_validate(result.data)
                notice.extraction_json = json.dumps(result.data, ensure_ascii=False)
                deadline = _parse_notice_date(extraction.deadline)
                if (
                    extraction.area_sqm is None
                    or extraction.starting_price is None
                    or extraction.currency is None
                    or deadline is None
                ):
                    notice.processed_at = run_at
                    notice.last_error = "incomplete_extraction"
                    processed += 1
                    db.commit()
                    continue
                if deadline < run_at:
                    notice.processed_at = run_at
                    notice.last_error = "deadline_expired"
                    processed += 1
                    db.commit()
                    continue
                price_eur = (
                    extraction.starting_price / EUR_BGN_RATE
                    if extraction.currency == "BGN"
                    else extraction.starting_price
                )
                price_bgn = (
                    extraction.starting_price
                    if extraction.currency == "BGN"
                    else extraction.starting_price * EUR_BGN_RATE
                )
                listing = repo.upsert(
                    {
                        "source": "municipal",
                        "source_id": f"{notice.source}:{notice.source_id}",
                        "listing_kind": "auction",
                        "url": notice.url,
                        "title": notice.title,
                        "price_eur": round(price_eur, 2),
                        "price_bgn": round(price_bgn, 2),
                        "area_sqm": float(extraction.area_sqm),
                        "price_per_sqm_eur": round(price_eur / extraction.area_sqm, 2),
                        "neighborhood": _neighborhood(extraction.address, combined),
                        "property_type": extraction.property_type,
                        "description": notice.title,
                        "description_full": text[:16_000],
                        "address": extraction.address,
                        "seller_type": "municipal",
                        "auction_start": notice.notice_date,
                        "auction_end": deadline,
                        "bailiff_name": "СОАПИ" if notice.source == "soapi" else "Столична община",
                        "case_number": _decision_number(notice.title),
                        "is_active": True,
                    },
                    commit=False,
                )
                notice.listing_id = listing.id
                notice.processed_at = run_at
                notice.last_error = None
                db.commit()
                created += 1
                processed += 1
            except Exception as exc:
                db.rollback()
                current = db.query(SeenNotice).filter_by(id=notice.id).one()
                current.last_error = str(exc)[:500]
                db.commit()
                failed += 1
                logger.warning(f"Municipal extraction failed for {notice.url}: {exc}")

        pending_count = db.query(SeenNotice).filter(
            SeenNotice.is_residential.is_(True),
            SeenNotice.processed_at.is_(None),
        ).count()
        return {
            "skipped": None,
            "new_notices": new_notices,
            "candidates": len(pending_rows),
            "processed": processed,
            "listings_created": created,
            "failed": failed,
            "pending": pending_count,
            "errors": list(self.source_errors),
        }


def _decision_number(title: str) -> str | None:
    match = re.search(r"(?:решение|търг)\s*№?\s*([\w./-]+)", title, re.IGNORECASE)
    return match.group(1) if match else None


def run_municipal_watcher(
    db: Session,
    *,
    provider: MunicipalProvider | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> Dict[str, Any]:
    with MunicipalNoticeWatcher() as watcher:
        return watcher.run(db, provider=provider, force=force, now=now)
