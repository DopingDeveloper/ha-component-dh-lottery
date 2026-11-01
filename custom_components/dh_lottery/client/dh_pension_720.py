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
import re
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
        """낱장 구매의 BUY_SET_TYPE은 자동/수동 모두 S."""
        return "S"


@dataclass
class DhPension720BuyHistoryData:
    round_no: int
    issue_dt: str
    barcode: str
    ticket_count: int
    amount: int
    result: str

    @property
    def purchase_number(self) -> str:
        """구매 이력의 gmInfo를 사람이 읽기 좋은 조/번호로 변환합니다."""
        match = re.fullmatch(r"\s*([1-5])\s*[:조]\s*(\d{1,6})\s*", self.barcode)
        if not match:
            return self.barcode
        return f"{match.group(1)}조 {int(match.group(2)):06d}"


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
        self._game_user_id: Optional[str] = None

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
        if self._jsessionid and self._game_user_id:
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

            user_id_match = re.search(
                r'name=["\']USER_ID["\'][^>]*value=["\']([^"\']*)',
                page,
                re.IGNORECASE,
            )
            if user_id_match and user_id_match.group(1):
                self._game_user_id = user_id_match.group(1)

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

        if not self._game_user_id:
            # 일부 페이지 변형에서는 USER_ID hidden input이 생략되지만 로그인
            # 계정 ID와 값이 동일하다. 라이브 계약에서는 hidden 값을 우선한다.
            self._game_user_id = self.client.username
            _LOGGER.warning(
                "[PENSION720] game.jsp USER_ID를 찾지 못해 로그인 ID를 사용합니다."
            )

        _LOGGER.info("[PENSION720] 구매용 게임 세션 확보 완료")

    def _reset_session(self):
        """세션 리셋 (재시도 시 사용)"""
        self._jsessionid = None
        self._game_user_id = None

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
        """서버가 배정한 동일번호를 1~5조 세트로 구매."""
        return await self._async_buy_set()

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

    @staticmethod
    def _wire_q(raw_q: str) -> str:
        """encrypt.js와 jQuery 전송을 합친 실제 q 와이어 인코딩."""
        return raw_q.replace("+", "%252B").replace("/", "%2F").replace("=", "%3D")

    async def _post_encrypted(self, path: str, plaintext: str) -> dict:
        """게임 서버 암호화 API를 한 번 호출하고 JSON 평문을 반환."""
        body = f"q={self._wire_q(self._enc(plaintext))}"
        try:
            resp = await self.client.session.post(
                f"{EL_BASE_URL}/{path}",
                data=body,
                headers={
                    "Origin": EL_BASE_URL,
                    "Referer": f"{EL_BASE_URL}/game/pension720/game.jsp",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
            payload = await resp.json(content_type=None)
        except Exception as ex:
            raise DhPension720PurchaseError(
                f"{path} 서버 응답 오류 — 구매 내역을 확인하세요."
            ) from ex

        if not isinstance(payload, dict):
            raise DhPension720PurchaseError(f"{path} 응답 형식 오류")

        encrypted = payload.get("q")
        if encrypted:
            try:
                return self._parse(self._dec(encrypted))
            except Exception as ex:
                raise DhPension720PurchaseError(
                    f"{path} 응답 복호화 오류 — 구매 내역을 확인하세요."
                ) from ex
        return payload

    @staticmethod
    def _require_success(result: dict, step: str) -> None:
        code = str(result.get("resultCode", ""))
        if code != "100":
            message = result.get("resultMsg") or result.get("resultMessage") or ""
            raise DhPension720PurchaseError(
                f"{step} 실패 (code={code or 'unknown'}, message={message})"
            )

    async def _assign_auto(self, round_no: int, group: int) -> tuple[int, int]:
        """makeAutoNo.do에서 조와 실제 6자리 자동 번호를 배정받음."""
        request = urlencode([
            ("ROUND", round_no),
            ("SEL_NO", ""),
            ("BUY_CNT", ""),
            ("AUTO_SEL_SET", "S"),
            ("SEL_CLASS", group),
            ("BUY_TYPE", "A"),
            ("ACCS_TYPE", "01"),
        ])
        result = await self._post_encrypted("makeAutoNo.do", request)
        self._require_success(result, "자동번호 배정")
        assigned_group = str(result.get("selClsNo", group)).split(",")[0]
        assigned_number = str(result.get("selLotNo", "")).split(",")[0]
        if not assigned_group.isdigit() or not assigned_number.isdigit() or len(assigned_number) != 6:
            raise DhPension720PurchaseError("자동번호 배정 응답 형식 오류")
        return int(assigned_group), int(assigned_number)

    async def _verify_manual(self, round_no: int, group: int, number: int) -> None:
        """수동 번호가 아직 구매 가능한지 확인."""
        number_text = f"{number:06d}"
        request = urlencode([
            ("ROUND", round_no),
            ("SEL_NO", number_text),
            ("BUY_CNT", "1"),
            ("AUTO_SEL_SET", "S"),
            ("SEL_CLASS", group),
            ("BUY_TYPE", "M"),
            ("ACCS_TYPE", "01"),
        ])
        result = await self._post_encrypted("checkVerifyNo.do", request)
        self._require_success(result, "수동번호 확인")
        tokens = str(result.get("resultMsg", "")).split("/")
        verify_yn = tokens[1].strip() if len(tokens) > 1 else str(result.get("verifyYn", ""))
        recommend_yn = tokens[2].strip() if len(tokens) > 2 else str(result.get("recommendYN", ""))
        if verify_yn.upper() != "Y" or recommend_yn.upper() == "Y":
            raise DhPension720PurchaseError(
                f"{group}조 {number_text} 번호는 이미 판매되었거나 구매할 수 없습니다."
            )

    async def _buy_one(self, round_no: int, ticket: PensionTicket) -> str:
        """번호 배정/검증 → 주문 생성 → connPro 실결제를 한 게임만 수행."""
        manual = not ticket.is_auto
        if manual:
            group = ticket.group
            number = int(ticket.number)
            await self._verify_manual(round_no, group, number)
        else:
            group, number = await self._assign_auto(round_no, ticket.group)

        number_text = f"{number:06d}"
        buy_type = "M" if manual else "A"
        order_request = urlencode([
            ("ROUND", round_no),
            ("SEL_NO", number_text),
            ("BUY_CNT", "1"),
            ("AUTO_SEL_SET", "S"),
            ("SEL_CLASS", group),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
        ])
        order = await self._post_encrypted("makeOrderNo.do", order_request)
        self._require_success(order, "주문 생성")
        order_no = str(order.get("orderNo", ""))
        order_date = str(order.get("orderDate", ""))
        if not order_no or not order_date:
            raise DhPension720PurchaseError("주문번호 또는 주문일시가 없습니다.")

        digits = list(number_text)
        purchase_request = urlencode([
            ("ROUND", round_no),
            ("FLAG", ""),
            ("BUY_KIND", "01"),
            ("BUY_NO", f"{group}{number_text}"),
            ("BUY_CNT", "1"),
            ("BUY_SET_TYPE", "S"),
            ("BUY_TYPE", buy_type),
            ("ACCS_TYPE", "01"),
            ("orderNo", order_no),
            ("orderDate", order_date),
            ("TRANSACTION_ID", ""),
            ("WIN_DATE", ""),
            ("USER_ID", self._game_user_id or self.client.username),
            ("PAY_TYPE", ""),
            ("resultErrorCode", ""),
            ("resultErrorMsg", ""),
            ("resultOrderNo", ""),
            ("WORKING_FLAG", "false"),
            ("NUM_CHANGE_TYPE", ""),
            ("auto_process", "N" if manual else "Y"),
            ("set_type", "S"),
            ("classnum", group),
            ("selnum", number_text),
            ("buytype", buy_type),
            ("num1", digits[0]),
            ("num2", digits[1]),
            ("num3", digits[2]),
            ("num4", digits[3]),
            ("num5", digits[4]),
            ("num6", digits[5]),
            ("DSEC", "0"),
            ("CLOSE_DATE", ""),
            ("verifyYN", "Y" if manual else "N"),
            ("curdeposit", "0"),
            ("curpay", "1000"),
        ])

        # connPro는 실제 결제이며 비멱등이므로 절대 자동 재시도하지 않습니다.
        purchase = await self._post_encrypted("connPro.do", purchase_request)
        result_code = str(purchase.get("resultCode", ""))
        if result_code != "100":
            message = purchase.get("resultMsg") or purchase.get("resultMessage") or ""
            raise DhPension720PurchaseError(
                f"구매 실패 (code={result_code or 'unknown'}, message={message}) — "
                "결과가 불명확하면 구매 내역을 먼저 확인하세요."
            )

        data = purchase.get("data")
        if not isinstance(data, dict):
            nested = purchase.get("resultMsg")
            if isinstance(nested, str) and nested.lstrip().startswith("{"):
                try:
                    nested_data = json.loads(nested)
                    data = nested_data.get("data", {}) if isinstance(nested_data, dict) else {}
                except json.JSONDecodeError:
                    data = {}
            else:
                data = {}
        info = str(data.get("prchsLtNoInfoLstCn", "")).split("|")
        sold_number = info[0] if len(info) > 0 and len(info[0]) == 6 else number_text
        sold_group = info[4] if len(info) > 4 and info[4] in {"1", "2", "3", "4", "5"} else str(group)
        return f"{sold_group}조 {sold_number}"

    async def _async_buy_set(self) -> DhPension720BuyData:
        """서버 SA 계약으로 동일번호 1~5조를 한 번의 connPro에서 구매."""
        self._check_buy_time()
        try:
            await self._ensure_session()
        except DhPension720Error:
            self._reset_session()
            await self.client.async_login()
            await self._ensure_session()

        round_info = await self.async_get_round_info()
        current_round = int(round_info.get("round", 0))
        if not current_round:
            raise DhPension720PurchaseError("회차 정보를 가져올 수 없습니다")
        remain = round_info.get("remainTime")
        if remain is not None and int(remain) <= 0:
            raise DhPension720PurchaseError("판매 마감되었습니다")

        assign_request = urlencode([
            ("ROUND", current_round),
            ("SEL_NO", ""),
            ("BUY_CNT", ""),
            ("AUTO_SEL_SET", "SA"),
            ("SEL_CLASS", ""),
            ("BUY_TYPE", "A"),
            ("ACCS_TYPE", "01"),
        ])
        assigned = await self._post_encrypted("makeAutoNo.do", assign_request)
        self._require_success(assigned, "동일번호 배정")
        number_text = str(assigned.get("selLotNo", "")).split(",")[0]
        if not number_text.isdigit() or len(number_text) != 6:
            raise DhPension720PurchaseError("동일번호 배정 응답 형식 오류")

        order_request = urlencode([
            ("ROUND", current_round),
            ("SEL_NO", number_text),
            ("BUY_CNT", "5"),
            ("AUTO_SEL_SET", "SA"),
            ("SEL_CLASS", ""),
            ("BUY_TYPE", "A"),
            ("ACCS_TYPE", "01"),
        ])
        order = await self._post_encrypted("makeOrderNo.do", order_request)
        self._require_success(order, "동일번호 주문 생성")
        order_no = str(order.get("orderNo", ""))
        order_date = str(order.get("orderDate", ""))
        if not order_no or not order_date:
            raise DhPension720PurchaseError("동일번호 주문번호 또는 주문일시가 없습니다.")

        digits = list(number_text)
        buy_no = ",".join(f"{group}{number_text}" for group in range(1, 6))
        purchase_request = urlencode([
            ("ROUND", current_round),
            ("FLAG", ""),
            ("BUY_KIND", "01"),
            ("BUY_NO", buy_no),
            ("BUY_CNT", "5"),
            ("BUY_SET_TYPE", "SA,SA,SA,SA,SA"),
            ("BUY_TYPE", "A,A,A,A,A,"),
            ("ACCS_TYPE", "01"),
            ("orderNo", order_no),
            ("orderDate", order_date),
            ("TRANSACTION_ID", ""),
            ("WIN_DATE", ""),
            ("USER_ID", self._game_user_id or self.client.username),
            ("PAY_TYPE", ""),
            ("resultErrorCode", ""),
            ("resultErrorMsg", ""),
            ("resultOrderNo", ""),
            ("WORKING_FLAG", "false"),
            ("NUM_CHANGE_TYPE", ""),
            ("auto_process", "Y"),
            ("set_type", "SA"),
            ("classnum", ""),
            ("selnum", number_text),
            ("buytype", "A"),
            ("num1", digits[0]),
            ("num2", digits[1]),
            ("num3", digits[2]),
            ("num4", digits[3]),
            ("num5", digits[4]),
            ("num6", digits[5]),
            ("DSEC", "0"),
            ("CLOSE_DATE", ""),
            ("verifyYN", "N"),
            ("curdeposit", "0"),
            ("curpay", "5000"),
        ])

        # 세트 connPro도 실제 결제이며 비멱등이므로 단 한 번만 호출합니다.
        purchase = await self._post_encrypted("connPro.do", purchase_request)
        result_code = str(purchase.get("resultCode", ""))
        if result_code != "100":
            message = purchase.get("resultMsg") or purchase.get("resultMessage") or ""
            raise DhPension720PurchaseError(
                f"동일번호 구매 실패 (code={result_code or 'unknown'}, message={message}) — "
                "결과가 불명확하면 구매 내역을 먼저 확인하세요."
            )

        data = purchase.get("data")
        if not isinstance(data, dict):
            nested = purchase.get("resultMsg")
            if isinstance(nested, str) and nested.lstrip().startswith("{"):
                try:
                    nested_result = json.loads(nested)
                    data = nested_result.get("data", {}) if isinstance(nested_result, dict) else {}
                except json.JSONDecodeError:
                    data = {}
            else:
                data = {}

        raw = str(data.get("prchsLtNoInfoLstCn", ""))
        rows = [row.strip() for row in re.split(r"[,;\n]+", raw) if row.strip()]
        tickets: List[str] = []
        seen_groups = set()
        for row in rows:
            fields = row.split("|")
            sold_number = fields[0] if len(fields) > 0 else ""
            sold_group = fields[4] if len(fields) > 4 else ""
            if (
                sold_number != number_text
                or sold_group not in {"1", "2", "3", "4", "5"}
                or sold_group in seen_groups
            ):
                raise DhPension720PurchaseError(
                    "동일번호 구매 응답 형식이 올바르지 않습니다 — 구매 내역을 확인하세요."
                )
            seen_groups.add(sold_group)
            tickets.append(f"{sold_group}조 {sold_number}")

        if not tickets:
            # 실제 서버는 세트 결제 성공(resultCode=100) 시에도 환경에 따라
            # prchsLtNoInfoLstCn을 생략한다. 번호는 makeAutoNo에서 확정됐고
            # 요청 자체가 1~5조 고정 세트이므로 정상 성공 결과로 구성한다.
            _LOGGER.warning(
                "[PENSION720] 동일번호 결제 성공 응답에 티켓 상세가 없어 "
                "서버 배정 번호로 결과를 구성합니다."
            )
            tickets = [f"{group}조 {number_text}" for group in range(1, 6)]

        return DhPension720BuyData(
            round_no=current_round,
            ticket_count=len(tickets),
            tickets=", ".join(tickets),
            fail_count=5 - len(tickets),
            fail_tickets="" if len(tickets) == 5 else "일부 조 결과는 구매 내역을 확인하세요.",
            amount=len(tickets) * 1000,
        )

    async def _async_buy(self, tickets: List[PensionTicket]) -> DhPension720BuyData:
        """검증된 브라우저 구매 계약으로 최대 5게임을 순차 구매."""
        self._check_buy_time()
        try:
            await self._ensure_session()
        except DhPension720Error:
            self._reset_session()
            await self.client.async_login()
            await self._ensure_session()

        round_info = await self.async_get_round_info()
        current_round = round_info.get("round", 0)
        if not current_round:
            raise DhPension720PurchaseError("회차 정보를 가져올 수 없습니다")
        remain = round_info.get("remainTime")
        if remain is not None and int(remain) <= 0:
            raise DhPension720PurchaseError("판매 마감되었습니다")

        _LOGGER.info(
            "[PENSION720] 구매 시작: round=%s, count=%s",
            current_round,
            len(tickets),
        )
        purchased: List[str] = []
        failure = ""
        for ticket in tickets:
            try:
                purchased.append(await self._buy_one(int(current_round), ticket))
            except Exception as ex:
                # connPro는 비멱등이므로 실패 후 남은 게임을 진행하거나 재시도하지 않습니다.
                failure = str(ex)
                if not purchased:
                    raise
                _LOGGER.warning(
                    "[PENSION720] %s게임 구매 후 중단: %s", len(purchased), failure
                )
                break

        fail_count = len(tickets) - len(purchased)
        return DhPension720BuyData(
            round_no=current_round,
            ticket_count=len(purchased),
            tickets=", ".join(purchased),
            fail_count=fail_count,
            fail_tickets=failure,
            amount=len(purchased) * 1000,
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
