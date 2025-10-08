import logging
from datetime import datetime
import requests
import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException, status, Depends, APIRouter, Cookie, Query
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from tqdm import tqdm
import os
from email.mime.text import MIMEText
from dotenv import load_dotenv

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import asyncio

cache = {}
CACHE_DURATION = timedelta(minutes=5)

logging.basicConfig(level=logging.INFO,format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class CountryNotFound(Exception):
    pass

class ServerError(Exception):
    pass

class UnexpectedError(Exception):
    pass

class MeteoAlertsAPI(object):
    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{0}"

    def __init__(self, country:str, province:str, language:str = "en-EN"):
        self.country = country.lower()
        self.province = province.lower()
        self.language = language
    
    def safeText(self, tag, default=None):
        return tag.text.strip() if tag else default
    
    def checkStatusCode(self, statusCode):
        if statusCode == 404:
            raise (CountryNotFound("País no encontrado. Por favor verifique el país."))
        elif statusCode >= 500:
            raise(ServerError("Ha ocurrido un error con el servidor."))
        elif statusCode != 200:
            raise(UnexpectedError("Error inesperado. Intentelo de nuevo más tarde."))

    async def alerts(self):
        alerts = {}
        try: 
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url.format(self.country),timeout=5) as resp:
                    self.checkStatusCode(resp.status)
                    content = await resp.text()
                    
                    soup = BeautifulSoup(content, "xml")
                    entries = soup.find_all(lambda tag: tag.name.endswith("entry"))
                    found_alert = False
                    total = len(entries)
                    logger.info(f"Procesando {total} alertas.")
                    for entry in tqdm(entries, desc="Procesando alertas", unit="alerta"):
                        area = entry.find("cap:areaDesc")
                        geocode = entry.find("cap:geocode")
                        notParsedLinks = entry.find_all("link")        
                        province = area.text if area else "Desconocido"
                        geocode_val = self.safeText(geocode.find("value") if geocode else None)

                        title = entry.title.text if entry.title else "Sin titulo"
                        date = entry.updated.text if entry.updated else "Sin fecha"

                        if self.province.lower().strip() in province.lower().strip():
                            found_alert = True
                            cap_link = None
                            region_link = None
                            province_link = None

                            for link in notParsedLinks:
                                link_href = link.get("href", "")
                                link_type = (link.get("type") or "").lower()
                                link_title = (link.get("title") or "").lower()

                                if link_type.lower().strip() == "application/cap+xml":
                                    cap_link = link_href
                                elif self.province.lower().strip() in link_title.lower().strip():
                                    province_link = link_href
                                elif self.country.lower().strip() in link_title.lower().strip():
                                    region_link = link_href
                            severity = None
                            description = None
                            instruction = None
                            effective = None
                            onset = None
                            expires = None
                            if cap_link:
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(cap_link,timeout=5) as capReqst:
                                        self.checkStatusCode(capReqst.status)
                                        capContent = await capReqst.text()
                                        capSoup = BeautifulSoup(capContent, "xml")
                                        soupAlert = capSoup.alert
                                        alertInfo = soupAlert.find("info")
                                        severity = self.safeText(alertInfo.find("severity"))
                                        title = self.safeText(alertInfo.find("headline"))
                                        description = self.safeText(alertInfo.find("description"))
                                        instruction = self.safeText(alertInfo.find("instruction"))
                                        effective = self.safeText(alertInfo.find("effective"))
                                        onset = self.safeText(alertInfo.find("effective"))
                                        expires = self.safeText(alertInfo.find("effective"))

                            else:
                                severity_tag = entry.find("cap:severity")
                                severity = severity_tag.text.strip().lower() if severity_tag else "n/a"

                                description_tag = entry.find("cap:event")
                                description = description_tag.text.strip().lower() if description_tag else "n/a"

                                instruction_tag = entry.find("cap:instruction")
                                instruction = instruction_tag.text.strip().lower() if instruction_tag else "n/a"

                                effective_tag = entry.find("cap:effective")
                                effective = effective_tag.text.strip().lower() if effective_tag else "n/a"

                                onset_tag = entry.find("cap:onset")
                                onset = onset_tag.text.strip().lower() if onset_tag else "n/a"

                                expires_tag = entry.find("cap:expires")
                                expires = expires_tag.text.strip().lower() if expires_tag else "n/a"

                            alerts[province] = [];
                            alerts.setdefault(province, []).append({
                                    "titulo":title,
                                    "description":description,
                                    "instruction":instruction,
                                    "effective":effective,
                                    "onset":onset,
                                    "expirese":expires,
                                    "fecha":date,
                                    "severity":severity,
                                    "geocode":geocode_val,
                                    "link":[cap_link,region_link,province_link],
                                    "timeStamp":datetime.now().timestamp()
                                })
                        
                    if not found_alert:
                        return {"empty": "no se han encontrado alertas en esta provincia"}
        except (asyncio.TimeoutError, aiohttp.ClientError, Exception) as e:
            logger.error(f"Timeout: {e}")
            logger.info(f"Ha ocurrido un error. {e}")
            return {"error":str(e)}

        return alerts



load_dotenv()
API_TOKEN = os.environ.get("AUTHORIZATION_TOKEN")
app = FastAPI(title="ZeroWeather MeteoAlarm API") 
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
alert_router = APIRouter(
    prefix="/alerts",
    tags=["alerts"]
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse({"error": "Too many requests"}, status_code=429)


def auth_dependency(request: Request, token: str | None = Query(default=None), access_token: str | None = Cookie(default=None)):
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        provided = auth_header.split("Bearer ")[1]
        if provided == API_TOKEN:
            return True
    if token and token == API_TOKEN:
        return True
    if access_token and access_token == API_TOKEN:
        return True
    raise HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail="No autorizado"
    )


async def get_cached_alerts(country, province):
    key = f"{country}_{province}"
    if key in cache:
        data, timestamp = cache[key]
        if datetime.now() - timestamp < CACHE_DURATION:
            return data 

    data = await MeteoAlertsAPI(country, province).alerts()
    cache[key] = (data, datetime.now())
    return data


#Api de datos alertas
@alert_router.get("/{country}/{province}")
@limiter.limit("3/minute")
async def get_alerts(request: Request, country: str, province: str, authorized: bool = Depends(auth_dependency)):
    result = await get_cached_alerts(country, province)
    return JSONResponse(content=result)

app.include_router(alert_router)