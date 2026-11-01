# -*- coding: utf-8 -*-
"""
DH Lottery Pension 720+ Client
연금복권 720+ 구매 및 정보 조회

암호화: AES-128-CBC + PBKDF2(SHA256, 1000iter) / key = game session id[:32]
Base URL: https://el.dhlottery.co.kr
Purchase Flow: makeOrderNo.do → connPro.do → checkDeposit.do
"""

import datetime
import logging
import base64
import json
import os
import random
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import urlencode, parse_qs

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from yarl import URL

from .dh_lottery_client import DhLotteryError

_LOGGER = logging.getLogger(__name__)

EL_BASE_URL = "https://el.dhlottery.co.kr"


class DhPension720Error(DhLotteryError):
    pass


class DhPension720PurchaseError(DhPension720Error):
    pass


@dataclass
class DhPension720BuyData:
    round_no: int
    ticket_count: int
    tickets: str
    fail_count: int
    fail_tickets: str
    amount: int


@dataclass
class PensionTicket:
    """연금복권 720+ 구매 티켓 (조 + 번호)"""
    group: int              # 조 (1~5)
    number: Optional[int] = None  # 6자리 번호 (0~999999), None = 자동(서버 선택)

    @property
    def is_auto(self) -> bool:
        return self.number is None

    def buy_no(self) -> str:
        """BUY_NO 형식: {조}{번호:06d}"""
        if self.number is None:
            return f"{self.group}000000"
        return f"{self.group}{self.number:06d}"

    def set_type(self) -> str:
        """BUY_SET_TYPE: SA=자동, SE=수동"""
        return "SA" if self.number is None else "SE"


@dataclass
class DhPension720BuyHistoryData:
    round_no: int
    issue_dt: str
    barcode: str
    ticket_count: int
    amount: int
    result: str


# ---------------------------------------------------------------------------
# AES encryption / decryption  (matches game.jsp encrypt() / decrypt())
# ---------------------------------------------------------------------------

def _encrypt(plaintext: str, jsessionid: str) -> str:
    passphrase = jsessionid[:32].encode("utf-8")
    salt = os.urandom(32)
    iv = os.urandom(16)

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=salt, iterations=1000
    ).derive(passphrase)

    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    # aiohttp form data performs the one URL-encoding pass used by the
    # browser's encrypt.js + raw `q=<encrypted>` request.
    return salt.hex() + iv.hex() + base64.b64encode(ciphertext).decode("ascii")


def _decrypt(enc_text: str, jsessionid: str) -> str:
    passphrase = jsessionid[:32].encode("utf-8")

    salt = bytes.fromhex(enc_text[:64])
    iv = bytes.fromhex(enc_text[64:96])
    ciphertext = base64.b64decode(enc_text[96:])

    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=salt, iterations=1000
    ).derive(passphrase)

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = sym_padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# Main client class
# ---------------------------------------------------------------------------

class DhPension720:
    """연금복권 720+ 클라이언트"""

    def __init__(self, client):
        self.client = client
        self._jsessionid: Optional[str] = None

    # ------------------------------------------------------------------
    # Session helpers - 로또와 동일한 세션 재사용, 게임 서버 세션 확보
    # ------------------------------------------------------------------

    async def _ensure_session(self):
        """el.dhlottery.co.kr 게임 서버 세션 쿠키 확보.

        동행복권 웹사이트가 사용하는 공식 진입점(TotalGame.jsp)을 먼저
        방문해 www.dhlottery.co.kr 로그인 세션을 게임 서버로 전달한 뒤,
        실제 연금복권 페이지에서 인증 상태를 확인합니다. 애드온과 동일하게
        실제 쿠키명인 DHJSESSIONID를 우선하고 JSESSIONID를 보조로 사용합니다.
        """
        if self._jsessionid:
            return

        entry_url = f"{EL_BASE_URL}/game/TotalGame.jsp?LottoId=LP72"
        game_url = f"{EL_BASE_URL}/game/pension720/game.jsp"
        cookie_names = ("DHJSESSIONID", "JSESSIONID")
        response_cookies = {}
        page = ""
        try:
            # 애드온과 동일하게 기존 게임 서버 쿠키를 먼저 재사용합니다.
            cookies = self.client.session.cookie_jar.filter_cookies(URL(EL_BASE_URL))
            for name in cookie_names:
                morsel = cookies.get(name)
                if morsel and getattr(morsel, "value", None):
                    self._jsessionid = morsel.value
                    break

            if self._jsessionid:
                _LOGGER.info("[PENSION720] 기존 구매용 게임 세션 확보 완료")
                return

            async with self.client.session.get(
                entry_url,
                allow_redirects=True,
                headers={"Referer": "https://www.dhlottery.co.kr/"},
            ) as resp:
                await resp.text()
                response_cookies.update(resp.cookies)

            async with self.client.session.get(
                game_url,
                allow_redirects=True,
                headers={"Referer": entry_url},
            ) as resp:
                page = await resp.text()
                response_cookies.update(resp.cookies)

            # Path 속성 때문에 cookie_jar 필터에서 제외되는 경우를 대비해
            # 애드온처럼 응답 Set-Cookie도 직접 확인합니다.
            for name in cookie_names:
                morsel = response_cookies.get(name)
                if morsel and getattr(morsel, "value", None):
                    self._jsessionid = morsel.value
                    break

            if not self._jsessionid:
                cookies = self.client.session.cookie_jar.filter_cookies(URL(EL_BASE_URL))
                for name in cookie_names:
                    morsel = cookies.get(name)
                    if morsel and getattr(morsel, "value", None):
                        self._jsessionid = morsel.value
                        break

            if (
                not self._jsessionid
                and ("로그인후 이용하세요" in page or "로그인 후 이용하세요" in page)
            ):
                raise DhPension720Error(
                    "연금복권 구매 서버 로그인 세션을 생성하지 못했습니다."
                )
        except Exception as e:
            self._jsessionid = None
            if isinstance(e, DhPension720Error):
                raise
            raise DhPension720Error(
                f"연금복권 구매 서버 접속 실패: {e}"
            ) from e

        if not self._jsessionid:
            raise DhPension720Error(
                "연금복권 구매용 게임 세션 쿠키를 가져올 수 없습니다."
            )

        _LOGGER.info("[PENSION720] 구매용 게임 세션 확보 완료")

    def _reset_session(self):
        """세션 리셋 (재시도 시 사용)"""
        self._jsessionid = None

    def _enc(self, form_data: str) -> str:
        return _encrypt(form_data, self._jsessionid)

    def _dec(self, enc_text: str) -> str:
        return _decrypt(enc_text, self._jsessionid)

    @staticmethod
    def _parse(decrypted: str) -> dict:
        """복호화된 JSON 또는 URL-encoded 응답을 dict로 변환."""
        stripped = decrypted.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        params = parse_qs(decrypted, keep_blank_values=True)
        return {k: v[0] if len(v) == 1 else v for k, v in params.items()}

    # ------------------------------------------------------------------
    # 구매 가능 시간 체크 (로또와 동일 방식)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_buy_time():
        """연금복권 구매 가능 시간 확인 (KST 기준)"""
        now = datetime.datetime.now()
        if now.hour < 6:
            raise DhPension720PurchaseError(
                "[ERROR] 구매 시간이 아닙니다. (06:00~24:00 구매 가능)"
            )
        if now.weekday() == 5 and now.hour >= 20:
            raise DhPension720PurchaseError(
                "[ERROR] 토요일 20:00 이후 구매 불가. (일요일 06:00부터 가능)"
            )

    # ------------------------------------------------------------------
    # Round info
    # ------------------------------------------------------------------

    async def async_get_round_info(self) -> dict:
        """현재 회차 정보 조회.

        1차: el.dhlottery.co.kr/roundRemainTime.do (JSESSIONID 필요)
        실패 시 2차: 공개 당첨결과의 최신 추첨 회차 + 1
        실패 시 3차: www.dhlottery.co.kr 구매이력에서 최근 회차 추출
        실패 시 4차: 첫 추첨일(2020-05-07)과 매주 목요일 주기로 판매 회차 계산
        """
        # 1차 시도: el.dhlottery.co.kr
        try:
            await self._ensure_session()
            async with self.client.session.get(
                f"{EL_BASE_URL}/roundRemainTime.do",
                headers={"Referer": f"{EL_BASE_URL}/game/pension720/game.jsp"},
            ) as resp:
                data = json.loads(await resp.text())
            if isinstance(data, dict) and data.get("round"):
                return data
            _LOGGER.warning(f"[PENSION720] roundRemainTime.do 응답 이상: {str(data)[:100]}")
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] 게임 서버 회차 조회 실패: {e}")

        # 2차 fallback: 로그인 없이 사용 가능한 공식 당첨결과 API
        try:
            data = await self.client.async_get(
                "pt720/selectPstPt720WnList.do", params={}
            )
            results = data.get("result", []) if isinstance(data, dict) else []
            latest_draw = max(
                (int(item.get("psltEpsd", 0)) for item in results), default=0
            )
            if latest_draw:
                current_round = latest_draw + 1
                _LOGGER.info(
                    "[PENSION720] 공개 당첨결과 기반 판매 회차: %s",
                    current_round,
                )
                return {
                    "round": current_round,
                    "remainTime": None,
                    "source": "public_results",
                }
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] 공개 회차 조회 실패: {e}")

        # 3차 fallback: 구매이력에서 최근 회차 추출
        try:
            items = await self.client.async_get_buy_list("LP72")
            if items:
                current_round = max(int(item.get("ltEpsd", 0)) for item in items)
                _LOGGER.info(f"[PENSION720] 구매이력 기반 회차: {current_round}")
                return {
                    "round": current_round,
                    "remainTime": None,
                    "source": "purchase_history",
                }
        except Exception as e:
            _LOGGER.warning(f"[PENSION720] 구매이력 회차 조회도 실패: {e}")

        # 4차 fallback: 연금복권 720+는 2020-05-07(목) 1회 추첨을
        # 시작으로 매주 목요일에 추첨한다. 외부 회차 API와 구매이력이
        # 모두 비어 있어도 다음 목요일의 판매 회차는 결정적이다.
        today = datetime.datetime.now().date()
        first_draw = datetime.date(2020, 5, 7)
        days_until_thursday = (3 - today.weekday()) % 7
        next_draw = today + datetime.timedelta(days=days_until_thursday)
        current_round = ((next_draw - first_draw).days // 7) + 1
        if current_round > 0:
            _LOGGER.warning(
                "[PENSION720] 공식 추첨 주기 기반 판매 회차 fallback: %s",
                current_round,
            )
            return {
                "round": current_round,
                "remainTime": None,
                "source": "calendar_fallback",
            }

        return {"round": 0, "remainTime": None}

    # ------------------------------------------------------------------
    # Purchase
    # ------------------------------------------------------------------

    async def async_buy_1(self) -> DhPension720BuyData:
        """1조 자동 1장 구매"""
        return await self._async_buy([PensionTicket(group=1)])

    async def async_buy_5(self) -> DhPension720BuyData:
        """1~5조 자동 5장 구매 (조별 다른 번호, 서버 선택)"""
        return await self._async_buy([PensionTicket(group=g) for g in range(1, 6)])

    async def async_buy_random_all_groups(self) -> DhPension720BuyData:
        """랜덤 번호 1개를 뽑아 1~5조 전부 동일 번호로 구매"""
        number = random.randint(0, 999999)
        _LOGGER.info(f"[PURCHASE] 동일번호 5조 구매 - 번호: {number:06d}")
        return await self._async_buy([PensionTicket(group=g, number=number) for g in range(1, 6)])

    async def async_buy_manual(self, tickets: List[PensionTicket]) -> DhPension720BuyData:
        """수동 구매: 조와 번호를 직접 지정

        Args:
            tickets: PensionTicket 리스트 (group=1~5, number=0~999999)
        """
        if not tickets:
            raise DhPension720PurchaseError("구매할 티켓이 없습니다")
        for t in tickets:
            if not (1 <= t.group <= 5):
                raise DhPension720PurchaseError(f"조 번호 범위 오류: {t.group} (1~5)")
            if t.number is not None and not (0 <= t.number <= 999999):
                raise DhPension720PurchaseError(f"번호 범위 오류: {t.number} (0~999999)")
        return await self._async_buy(tickets)

    async def _async_buy(self, tickets: List[PensionTicket]) -> DhPension720BuyData:
        """
        연금복권 720+ 구매 (자동/수동 통합)

        Flow:
          0. 구매 시간 확인
          1. roundRemainTime.do  → 현재 회차 / 잔여시간
          2. makeOrderNo.do      → 주문번호 생성 (encrypted)
          3. connPro.do          → 구매 실행   (encrypted)
          4. checkDeposit.do     → 잔액 확인   (encrypted)
        """
        # ── Step 0: 구매 시간 확인 ────────────────────
        self._check_buy_time()

        # ── 세션 확보 (DHJSESSIONID/JSESSIONID) ──────
        try:
            await self._ensure_session()
        except DhPension720Error:
            self._reset_session()
            await self.client.async_login()
            await self._ensure_session()

        ticket_count = len(tickets)
        is_manual = any(not t.is_auto for t in tickets)
        buy_type = "S" if is_manual else "A"

        # ── Step 1: 회차 확인 ─────────────────────────
        round_info = await self.async_get_round_info()
        current_round = round_info.get("round", 0)
        if not current_round:
            raise DhPension720PurchaseError("회차 정보를 가져올 수 없습니다")
        remain = round_info.get("remainTime")
        if remain is not None and int(remain) <= 0:
            raise DhPension720PurchaseError("판매 마감되었습니다")

        _LOGGER.info(
            f"[PURCHASE] round={current_round}, remainTime={remain}, "
            f"tickets={[t.buy_no() for t in tickets]}, manual={is_manual}"
        )

        # ── frmauto: makeOrderNo.do용 파라미터 ────────
        # 수동: 대표 번호/조 전달 (단일이면 그대로, 다중이면 첫 번째)
        first_manual = next((t for t in tickets if not t.is_auto), None)
        frmauto = urlencode([
            ("ROUND", current_round),
            ("SEL_NO", f"{first_manual.number:06d}" if first_manual else ""),
            ("BUY_CNT", ""),
            ("AUTO_SEL_SET", ""),
            ("SEL_CLASS", str(first_manual.group) if first_manual and len(tickets) == 1 else ""),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
        ])

        # ── Step 2: makeOrderNo.do ────────────────────
        resp1 = await self.client.session.post(
            f"{EL_BASE_URL}/makeOrderNo.do",
            data={"q": self._enc(frmauto)},
        )
        r1 = await resp1.json(content_type=None)
        if "q" not in r1:
            raise DhPension720PurchaseError(f"makeOrderNo 응답 오류: {r1}")

        p1 = self._parse(self._dec(r1["q"]))
        order_no = p1.get("orderNo", "")
        if not order_no:
            result_code = p1.get("resultCode", p1.get("errorCode", "unknown"))
            result_message = p1.get(
                "resultMsg", p1.get("errorMsg", p1.get("message", ""))
            )
            response_keys = ",".join(sorted(p1.keys())) or "none"
            raise DhPension720PurchaseError(
                "주문번호 생성 실패 "
                f"(code={result_code}, message={result_message}, keys={response_keys})"
            )
        _LOGGER.info(f"[PURCHASE] orderNo={order_no}")

        # ── Step 3: connPro.do ────────────────────────
        buy_nos = [t.buy_no() for t in tickets]
        buy_set_types = [t.set_type() for t in tickets]

        # 수동일 때 단일 필드 (마지막 수동 티켓 기준)
        last_manual = next((t for t in reversed(tickets) if not t.is_auto), None)
        set_type_val = "SE" if last_manual else "SA"
        classnum_val = str(last_manual.group) if last_manual else ""
        selnum_val = f"{last_manual.number:06d}" if last_manual else ""
        num_digits = list(f"{last_manual.number:06d}") if last_manual else [""] * 6

        frm = urlencode([
            ("ROUND", current_round),
            ("FLAG", ""),
            ("BUY_KIND", "01"),
            ("BUY_NO", ",".join(buy_nos)),
            ("BUY_CNT", ticket_count),
            ("BUY_SET_TYPE", ",".join(buy_set_types)),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
            ("orderNo", order_no),
            ("orderDate", p1.get("orderDate", "")),
            ("TRANSACTION_ID", ""),
            ("WIN_DATE", ""),
            ("USER_ID", self.client.username),
            ("PAY_TYPE", ""),
            ("resultErrorCode", ""),
            ("resultErrorMsg", ""),
            ("resultOrderNo", ""),
            ("WORKING_FLAG", "false"),
            ("NUM_CHANGE_TYPE", ""),
            ("auto_process", ""),
            ("set_type", set_type_val),
            ("classnum", classnum_val),
            ("selnum", selnum_val),
            ("buytype", buy_type),
            ("num1", num_digits[0]),
            ("num2", num_digits[1]),
            ("num3", num_digits[2]),
            ("num4", num_digits[3]),
            ("num5", num_digits[4]),
            ("num6", num_digits[5]),
            ("DSEC", "0"),
            ("CLOSE_DATE", ""),
            ("verifyYN", "N"),
            ("curdeposit", "0"),
            ("curpay", "0"),
        ])

        resp2 = await self.client.session.post(
            f"{EL_BASE_URL}/connPro.do",
            data={"q": self._enc(frm)},
        )
        r2 = await resp2.json(content_type=None)
        if "q" not in r2:
            raise DhPension720PurchaseError(f"connPro 응답 오류: {r2}")

        p2 = self._parse(self._dec(r2["q"]))
        result_code = p2.get("resultCode", "")
        sale_cnt = int(p2.get("saleCnt", "0"))
        sale_ticket = p2.get("saleTicket", "")
        fail_cnt = int(p2.get("failCnt", "0"))
        fail_ticket = p2.get("failTicket", "")

        _LOGGER.info(
            f"[PURCHASE] resultCode={result_code}, saleCnt={sale_cnt}, failCnt={fail_cnt}"
        )

        if result_code == "120":
            raise DhPension720PurchaseError(f"구매 전체 실패: {fail_ticket}")
        if result_code not in ("100", "110"):
            raise DhPension720PurchaseError(f"구매 실패 (code={result_code})")
        if result_code == "110":
            _LOGGER.warning(f"[PURCHASE] 일부 실패: {fail_cnt}건 - {fail_ticket}")

        # ── Step 4: checkDeposit.do ───────────────────
        try:
            resp3 = await self.client.session.post(
                f"{EL_BASE_URL}/checkDeposit.do",
                data={"q": self._enc(frmauto)},
            )
            r3 = await resp3.json()
            if "q" in r3:
                self._parse(self._dec(r3["q"]))
        except Exception as e:
            _LOGGER.warning(f"[PURCHASE] checkDeposit 오류 (무시): {e}")

        return DhPension720BuyData(
            round_no=current_round,
            ticket_count=sale_cnt,
            tickets=sale_ticket,
            fail_count=fail_cnt,
            fail_tickets=fail_ticket,
            amount=sale_cnt * 1000,
        )

    # ------------------------------------------------------------------
    # History (www.dhlottery.co.kr 경유 - 로또와 동일한 API)
    # ------------------------------------------------------------------

    async def async_get_buy_history(self) -> List[DhPension720BuyHistoryData]:
        """구매 이력 조회 (www.dhlottery.co.kr - 로또와 동일 엔드포인트)"""
        try:
            items = await self.client.async_get_buy_list("LP72")
            return [
                DhPension720BuyHistoryData(
                    round_no=item.get("ltEpsd", 0),
                    issue_dt=item.get("issueDay", ""),
                    barcode=item.get("gmInfo", ""),
                    ticket_count=item.get("prchsQty", 0),
                    amount=item.get("ntslAmt", 0),
                    result=item.get("ltWnResult", "미추첨"),
                )
                for item in items
            ]
        except Exception as e:
            _LOGGER.error(f"구매 이력 조회 오류: {e}")
            raise DhPension720Error(f"구매 이력 조회 실패: {e}")
