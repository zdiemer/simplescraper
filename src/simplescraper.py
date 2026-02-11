from __future__ import annotations

import asyncio
import os
import random
import urllib.parse
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import aiohttp
import aiohttp.client_exceptions
from fake_headers import Headers
from loguru import logger

from helpers.playwright_wrapper import request as playwright_request


class ResponseNotOkError(Exception):
    pass


class ImmediatelyStopStatusError(Exception):
    pass


class DatePart(Enum):
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    WEEK = 5
    MONTH = 6
    YEAR = 7


class RateLimit:
    max_req: int
    per: DatePart
    rate_limit_per_route: bool
    get_route_path: Optional[Callable[[str], str]]
    range_req: Optional[Tuple[int, int]]
    per_multiplier: int

    def __init__(
        self,
        max_req: int = 1,
        per: DatePart = DatePart.SECOND,
        rate_limit_per_route: bool = False,
        get_route_path: Optional[Callable[[str], str]] = None,
        range_req: Optional[Tuple[int, int]] = None,
        per_multipler: int = 1,
    ):
        self.max_req = max_req
        self.per = per
        self.rate_limit_per_route = rate_limit_per_route
        self.get_route_path = get_route_path
        self.range_req = range_req
        self.per_multiplier = per_multipler

        if self.max_req <= 0:
            raise ValueError("`max_req` must be a positive number greater than zero")

        if self.rate_limit_per_route and self.get_route_path is None:
            raise ValueError(
                "Must specify `get_route_path` when `rate_limit_per_route` is True"
            )

        if self.range_req is not None:
            if self.range_req[0] >= self.range_req[1]:
                raise ValueError(
                    "When specifying `range_req`, first value "
                    "must be smaller than the second value"
                )
            if self.range_req[0] <= 0:
                raise ValueError(
                    "`range_req` lower bound must be a positive number greater than zero"
                )


class ExponentialBackoff:
    max_backoffs: int
    backoff_seconds: int
    exponent: int
    _backoffs: int

    def __init__(
        self, initial_backoff: int = 2, exponent: int = 2, max_backoffs: int = 3
    ):
        self.backoff_seconds = initial_backoff
        self.exponent = exponent
        self.max_backoffs = max_backoffs
        self._backoffs = 0

    async def backoff(self, url: str, reason: Union[int, str]):
        if self._backoffs + 1 > self.max_backoffs:
            raise ResponseNotOkError(reason)
        logger.warning(
            "Backing off for <red>{}</red>s for {} due to <red>{}</red>",
            self.backoff_seconds,
            url,
            reason,
        )
        await asyncio.sleep(self.backoff_seconds + random.random())
        self.backoff_seconds **= self.exponent
        self._backoffs += 1


class RateLimiter:
    settings: RateLimit
    _last_calls: Dict[str, datetime]

    def __init__(self, limit: RateLimit = RateLimit()):
        self.settings = limit
        self._last_calls = {}

    @property
    def seconds_between_requests(self) -> float:
        per = self.settings.per
        _max = self.settings.max_req

        if self.settings.range_req is not None:
            a, b = self.settings.range_req
            _max = random.randint(a, b)

        if per == DatePart.SECOND:
            return 1.0 / (_max / (1.0 * self.settings.per_multiplier))
        if per == DatePart.MINUTE:
            return 1.0 / (_max / (60.0 * self.settings.per_multiplier))
        if per == DatePart.HOUR:
            return 1.0 / (_max / (3600.0 * self.settings.per_multiplier))
        if per == DatePart.DAY:
            return 1.0 / (_max / (86_400.0 * self.settings.per_multiplier))
        if per == DatePart.WEEK:
            return 1.0 / (_max / (604_800.0 * self.settings.per_multiplier))
        if per == DatePart.MONTH:
            return 1.0 / (_max / (2.628e6 * self.settings.per_multiplier))
        if per == DatePart.YEAR:
            return 1.0 / (_max / (3.154e7 * self.settings.per_multiplier))

        return 0.0

    def next_call(self, key: str, now: datetime = datetime.now(UTC)) -> datetime:
        if key not in self._last_calls:
            return now

        return self._last_calls[key] + timedelta(
            seconds=self.seconds_between_requests + random.random()
        )

    async def request(
        self, url: str, func: Coroutine[Any, Any, Union[str, Any]]
    ) -> Union[str, Any]:
        key = (
            urllib.parse.urlparse(url).netloc
            if not self.settings.rate_limit_per_route
            else self.settings.get_route_path(url)  # type: ignore
        )

        utcnow = datetime.now(UTC)
        next_call = self.next_call(key, utcnow)

        if next_call > utcnow:
            delta = next_call - utcnow
            sleep_time_seconds = delta.total_seconds()
            if sleep_time_seconds >= 5.0:
                logger.debug(
                    "Throttling <yellow>{}</yellow>s for {}",
                    f"{sleep_time_seconds:,.2f}",
                    url,
                )
            await asyncio.sleep(sleep_time_seconds)

        self._last_calls[key] = datetime.now(UTC)
        return await func()  # type: ignore


class Scraper:
    __SPOOF_HEADER_LIFETIME_MINUTES: int = 60

    __cached_headers: Optional[dict]
    __cached_responses: Dict[int, Union[Any, str]]
    __default_headers: Dict[str, str]
    __immediately_stop_statuses: List[int]
    __next_headers: datetime
    __spoof_headers: bool

    _rate_limiter: RateLimiter

    base_url: Optional[str]
    use_playwright: bool
    use_proxy: bool
    proxy_list: List[str]

    CACHE_FILE_NAME = "cache.pkl"

    def __init__(
        self,
        limit: RateLimit = RateLimit(),
        spoof_headers: bool = False,
        immediately_stop_statuses: Optional[List[int]] = None,
        base_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        use_playwright: bool = False,
        use_proxy: bool = False,
    ):
        self.base_url = base_url

        if user_agent is not None:
            self.__default_headers = {"User-Agent": user_agent}
        else:
            self.__default_headers = {}

        self._rate_limiter = RateLimiter(limit)
        self.__spoof_headers = spoof_headers
        self.__next_headers = datetime.now(UTC)
        self.__cached_headers = None
        self.__cached_responses = {}
        self.__immediately_stop_statuses = immediately_stop_statuses or []
        self.use_playwright = use_playwright
        self.use_proxy = use_proxy
        self.proxy_list = []

        if not os.path.exists("proxies_list.txt"):
            logger.info("No proxies_list.txt found, loading these may take a moment...")
            # Imported here because it eagerly loads proxies on import
            from multiproxies import proxies

            for ip, port in zip(proxies.ips, proxies.ports):
                self.proxy_list.append(f"http://{ip}:{port}")
        else:
            with open("proxies_list.txt", "r") as f:
                for line in f:
                    self.proxy_list.append(f"http://{line.strip()}")

    def _get_headers(self, url: str) -> dict:
        if self.__cached_headers is None or datetime.now(UTC) > self.__next_headers:
            new_headers = Headers().generate()
            new_headers["Referer"] = random.choice(
                [
                    "https://www.google.com/",
                    "https://www.bing.com/",
                    "https://search.yahoo.com/",
                    "https://duckduckgo.com/",
                    urllib.parse.urljoin(url, urllib.parse.urlparse(url).path),
                    "https://twitter.com/",
                ]
            )
            new_headers["Accept-Encoding"] = "gzip, deflate, br"
            new_headers["Accept-Language"] = "en-US,en;q=0.9,ja;q=0.8"
            new_headers["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            )
            self.__cached_headers = new_headers
            logger.debug(
                "Refreshing spoofed headers with User-Agent: <blue>{}</blue>",
                self.__cached_headers.get("User-Agent"),
            )
            self.__next_headers = datetime.now(UTC) + timedelta(
                minutes=self.__SPOOF_HEADER_LIFETIME_MINUTES
            )
        return self.__cached_headers

    def _hash_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
    ) -> int:
        return hash(
            url
            + (
                ",".join([str(v) for v in params.values()])
                if params is not None
                else ""
            )
            + (str(data) if data is not None else "")
            + (str(json_body) if json_body is not None else "")
        )

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Any = None,
        json: bool = True,
        retry_errors: Optional[List[Exception]] = None,
        json_body: Any = None,
        no_cache: bool = False,
    ) -> Union[Any, str]:
        req_hash = self._hash_request(url, params, data, json_body)

        if req_hash in self.__cached_responses and not no_cache:
            logger.debug("Serving {} from cache", url)
            return self.__cached_responses[req_hash]

        if headers is None:
            headers = (
                self.__default_headers
                if not self.__spoof_headers
                else self._get_headers(url)
            )
        elif self.__spoof_headers:
            headers = {**self.__default_headers, **headers}
            headers.update(self._get_headers(url))

        backoff = ExponentialBackoff()

        async def do_req():
            try:
                if self.use_playwright:
                    return await playwright_request(
                        url,
                        random.choice(self.proxy_list)
                        if self.use_proxy and self.proxy_list
                        else None,
                    )

                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        data=data,
                        json=json_body,
                        proxy=random.choice(self.proxy_list)
                        if self.use_proxy and self.proxy_list
                        else None,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as res:
                        if res.status != 200:
                            if res.status in self.__immediately_stop_statuses:
                                raise ImmediatelyStopStatusError
                            text = await res.text()
                            await backoff.backoff(str(res.url), text or res.status)
                            return await do_req()
                        res_val = await res.json() if json else await res.text()
                        if res_val is None:
                            await backoff.backoff(url, "Empty response")
                            return await do_req()
                        self.__cached_responses[req_hash] = res_val
                        return res_val
            except Exception as exc:
                if (
                    type(exc)
                    in (
                        aiohttp.client_exceptions.ClientError,
                        aiohttp.client_exceptions.ClientOSError,
                        aiohttp.client_exceptions.ClientConnectionError,
                        aiohttp.client_exceptions.ClientPayloadError,
                        aiohttp.client_exceptions.ClientConnectorError,
                        aiohttp.client_exceptions.ServerDisconnectedError,
                        asyncio.TimeoutError,
                        ConnectionError,
                    )
                    or retry_errors is not None
                    and type(exc) in retry_errors
                ):
                    print(exc)
                    await backoff.backoff(url, type(exc).__name__)
                    return await do_req()
                raise

        return await self._rate_limiter.request(url, do_req)  # type: ignore

    async def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        json: bool = True,
        retry_errors: Optional[List[Exception]] = None,
        json_body: Any = None,
        no_cache: bool = False,
    ) -> Union[Any, str]:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            json=json,
            retry_errors=retry_errors,
            json_body=json_body,
            no_cache=no_cache,
        )

    async def post(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Any = None,
        json: bool = True,
        retry_errors: Optional[List[Exception]] = None,
        json_body: Any = None,
        no_cache: bool = False,
    ) -> Union[Any, str]:
        return await self.request(
            "POST",
            url,
            params=params,
            headers=headers,
            data=data,
            json=json,
            retry_errors=retry_errors,
            json_body=json_body,
            no_cache=no_cache,
        )
