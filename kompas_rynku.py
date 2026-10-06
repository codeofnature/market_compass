#!/usr/bin/env python3
"""
KOMPAS RYNKU - kwartalny przegląd stanu cyklu. Barometr, nie zegarek.

Uruchomienie:
    python kompas_rynku.py          # prawdziwe dane z FRED (potrzebny internet)
    python kompas_rynku.py --demo   # dane syntetyczne - test, czy wszystko działa

Wymagania (jednorazowo):  pip install pandas

Wyniki trafiają do folderu "kompas_wyniki" obok skryptu:
    kompas_RRRR-MM-DD.html - raport do otwarcia w przeglądarce
    kompas_najnowszy.html  - kopia ostatniego raportu (stała nazwa do zakładek)
    kompas_historia.csv    - dopisywany wiersz przy każdym uruchomieniu
                             (zapis tego, co kompas mówił W DANYM DNIU -
                             tego nie da się "poprawić" po fakcie)
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# =====================================================================
# KONFIGURACJA - tu zmieniasz progi i wartości ręczne
# =====================================================================

# CAPE nie ma darmowego, stabilnego źródła CSV. Raz na kwartał przepisz
# wartość z https://www.multpl.com/shiller-pe (zmienia się powoli).
CAPE_RECZNIE = 41.4
CAPE_DATA = "2026-06-12"

# Progi stref (zielony < pierwszy próg <= żółty < drugi próg <= czerwony).
# Dla wszystkich poniższych "więcej = gorzej".
PROGI = {
    "CAPE": (20, 30),          # średnia historyczna ~17, rekord 44 (1999)
    "VIX": (20, 30),           # strach 30-50, panika ~80
    "Sahm": (0.3, 0.5),        # >= 0,5 = recesja potwierdzona
    "HY_poziom": (4.5, 6.0),   # % ponad obligacje skarbowe
    "HY_zmiana3m": (0.75, 1.5),  # wzrost spreadu w 3 mies. [pp]
}
KRZYWA_PLASKA = 0.5   # krzywa dodatnia, ale poniżej 0,5 pp = "płaska"
TREND_BUFOR = 2.0     # do 2% nad średnią 10-mies. = "blisko przecięcia"
CAPE_STARE_DNI = 120  # ostrzeżenie, jeśli ręczne CAPE jest starsze

LAT_NA_WYKRESIE = 5
DANE_OD = "2015-01-01"
FOLDER = Path(__file__).resolve().parent / "kompas_wyniki"

# Stopa referencyjna NBP: (od kiedy obowiązuje, wartość w %). NBP nie ma
# wygodnego API z historią stóp, więc historia jest tutaj. Skrypt sam
# sprawdza aktualną stopę w NBP i podpowie, gdy trzeba dopisać zmianę.
STOPA_REF_ZMIANY = [
    ("2020-05-29", 0.10), ("2021-10-07", 0.50), ("2021-11-04", 1.25), ("2021-12-09", 1.75),
    ("2022-01-05", 2.25), ("2022-02-09", 2.75), ("2022-03-09", 3.50), ("2022-04-07", 4.50),
    ("2022-05-06", 5.25), ("2022-06-09", 6.00), ("2022-07-08", 6.50), ("2022-09-08", 6.75),
    ("2023-09-07", 6.00), ("2023-10-05", 5.75), ("2025-05-08", 5.25), ("2025-07-03", 5.00),
    ("2025-09-04", 4.75), ("2025-10-09", 4.50), ("2025-11-06", 4.25), ("2025-12-04", 4.00),
    ("2026-03-05", 3.75),
]
CEL_INFLACYJNY = (1.5, 3.5)  # cel NBP 2,5% ± 1 pp

# Alias miesiąca: nowe pandas używa "ME", starsze "M".
try:
    pd.Series([1.0], index=pd.to_datetime(["2020-01-31"])).resample("ME")
    MIESIAC = "ME"
except ValueError:
    MIESIAC = "M"


# =====================================================================
# POBIERANIE DANYCH
# =====================================================================

def pobierz_fred(seria: str) -> pd.Series:
    """Pobiera serię z FRED.

    Z kluczem API (zmienna środowiskowa FRED_API_KEY) używa oficjalnego
    API - to jedyna droga, która działa niezawodnie z serwerów, np. z
    GitHub Actions. Bez klucza pobiera zwykły CSV ze strony wykresu,
    co zwykle działa z domowego komputera.
    """
    klucz = os.environ.get("FRED_API_KEY", "").strip()
    if klucz:
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={seria}&api_key={klucz}&file_type=json"
               f"&observation_start={DANE_OD}")
    else:
        url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv"
               f"?id={seria}&cosd={DANE_OD}")
    zapytanie = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (kompas-rynku)"})
    with urllib.request.urlopen(zapytanie, timeout=30) as odp:
        tekst = odp.read().decode("utf-8")
    if klucz:
        return json_na_serie(json.loads(tekst), seria)
    return csv_na_serie(tekst, seria)


def json_na_serie(obj: dict, nazwa: str) -> pd.Series:
    """Odpowiedź API: lista obserwacji {"date": ..., "value": ...};
    braki danych oznaczone kropką, jak w CSV."""
    obs = obj.get("observations", [])
    s = pd.Series([o["value"] for o in obs],
                  index=pd.to_datetime([o["date"] for o in obs]), name=nazwa)
    s = pd.to_numeric(s, errors="coerce").dropna().sort_index()
    if s.empty:
        raise ValueError("API FRED nie zwróciło danych")
    return s


def csv_na_serie(tekst: str, nazwa: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(tekst), na_values=".")
    daty = pd.to_datetime(df.iloc[:, 0])
    wartosci = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s = pd.Series(wartosci.values, index=daty, name=nazwa).dropna()
    if s.empty:
        # Zamiast CSV przyszła np. strona HTML z blokadą dla botów.
        raise ValueError("FRED zwrócił pustą odpowiedź lub stronę HTML zamiast CSV")
    return s.sort_index()


def dane_demo() -> dict:
    """Dane syntetyczne - tylko do sprawdzenia, czy skrypt działa.
    Kształt przypomina prawdziwe serie (inwersja 2022-24, szok 2020)."""
    rng = np.random.default_rng(42)
    dni = pd.bdate_range(DANE_OD, date.today())
    t = np.arange(len(dni))
    rok = dni.year + dni.dayofyear / 365

    krzywa = 1.2 - 2.0 * np.exp(-((rok - 2023.3) / 0.9) ** 2) \
        + rng.normal(0, 0.05, len(t))
    hy = 3.6 + 4.5 * np.exp(-((rok - 2020.25) / 0.12) ** 2) \
        - 0.6 * (rok > 2024) + rng.normal(0, 0.05, len(t))
    vix = 15 + 50 * np.exp(-((rok - 2020.22) / 0.05) ** 2) \
        + np.abs(rng.normal(0, 2.5, len(t)))
    zwroty = rng.normal(0.0004, 0.011, len(t))
    zwroty[(rok > 2020.15) & (rok < 2020.25)] -= 0.012
    sp500 = 2000 * np.exp(np.cumsum(zwroty))

    mies = pd.date_range(DANE_OD, date.today(), freq=MIESIAC)
    rokm = mies.year + mies.month / 12
    sahm = 0.1 + 2.5 * np.exp(-((rokm - 2020.4) / 0.25) ** 2)

    return {
        "T10Y3M": pd.Series(krzywa, index=dni),
        "BAMLH0A0HYM2": pd.Series(hy, index=dni),
        "VIXCLS": pd.Series(vix, index=dni),
        "SP500": pd.Series(sp500, index=dni),
        "SAHMREALTIME": pd.Series(sahm, index=mies),
    }


# =====================================================================
# OCENA WSKAŹNIKÓW - każda funkcja zwraca (strefa, wartość, komentarz)
# =====================================================================

def strefa(x: float, progi: tuple) -> str:
    """Strefa dla wskaźnika typu "więcej = gorzej"."""
    if x >= progi[1]:
        return "CZERWONY"
    if x >= progi[0]:
        return "ŻÓŁTY"
    return "ZIELONY"


def ocena_krzywej(s: pd.Series):
    # Średnie miesięczne zamiast dziennych: pojedynczy dzień poniżej zera
    # to szum, a nie inwersja.
    m = s.resample(MIESIAC).mean().dropna()
    teraz = s.iloc[-1]
    ost12 = m[m.index > m.index[-1] - pd.DateOffset(months=12)]
    ost24 = m[m.index > m.index[-1] - pd.DateOffset(months=24)]
    if teraz < 0:
        return "CZERWONY", teraz, "odwrócona - historycznie wyprzedza recesję o 12-18 mies."
    if (ost12 < 0).any():
        # Pułapka z notatek Iwucia: recesja często zaczyna się PO powrocie
        # krzywej do normy, więc wyjście z inwersji to nie jest "już bezpiecznie".
        return "CZERWONY", teraz, "wyjście z inwersji w ost. 12 mies. - typowe okno recesji"
    if (ost24 < 0).any():
        return "ŻÓŁTY", teraz, "inwersja w ost. 24 mies."
    if teraz < KRZYWA_PLASKA:
        return "ŻÓŁTY", teraz, "płaska"
    return "ZIELONY", teraz, "dodatnia, bez inwersji w ost. 24 mies."


def ocena_hy(s: pd.Series):
    # Liczy się nie tylko poziom, ale i TEMPO rozszerzania - to ono bywa
    # pierwszym sygnałem stresu kredytowego.
    teraz = s.iloc[-1]
    przed = s[s.index <= s.index[-1] - pd.DateOffset(months=3)]
    zmiana = teraz - przed.iloc[-1] if len(przed) else 0.0
    z = max(strefa(teraz, PROGI["HY_poziom"]),
            strefa(zmiana, PROGI["HY_zmiana3m"]),
            key=["ZIELONY", "ŻÓŁTY", "CZERWONY"].index)
    opis = {"CZERWONY": "stres kredytowy", "ŻÓŁTY": "spready się rozszerzają"}
    kom = opis.get(z, "bardzo ciasno - samozadowolenie (późny cykl)"
                   if teraz < 3.0 else "spokojnie")
    return z, teraz, f"{kom}; zmiana 3 mies. {zmiana:+.2f} pp"


def ocena_sahm(s: pd.Series):
    teraz = s.iloc[-1]
    z = strefa(teraz, PROGI["Sahm"])
    kom = {"CZERWONY": "recesja potwierdzona (wskaźnik opóźniający)",
           "ŻÓŁTY": "bezrobocie rośnie"}.get(z, "rynek pracy bez sygnału recesji")
    # Data odczytu pokazuje, czy raport z rynku pracy był już opublikowany.
    return z, teraz, f"{kom} (dane za {s.index[-1]:%m.%Y})"


def ocena_vix(s: pd.Series):
    teraz = s.iloc[-1]
    z = strefa(teraz, PROGI["VIX"])
    kom = {"CZERWONY": "strach na rynku", "ŻÓŁTY": "podwyższona nerwowość"}.get(
        z, "bardzo spokojnie - typowe dla euforii" if teraz < 13 else "spokojnie")
    return z, teraz, kom


def zakonczone_miesiace(s: pd.Series) -> pd.Series:
    """Zamknięcia miesięczne bez bieżącego, niezakończonego miesiąca.

    Reguła 10 miesięcy liczy się na zamknięciach miesięcy. Kompas rusza
    8. dnia, więc bieżący miesiąc ma dopiero kilka sesji - odrzucamy go,
    żeby wynik nie zależał od dnia uruchomienia.
    """
    mies = s.resample(MIESIAC).last().dropna()
    if len(mies) and mies.index[-1].to_period("M") == pd.Timestamp(date.today()).to_period("M"):
        mies = mies.iloc[:-1]
    return mies


def ocena_trendu(s: pd.Series):
    mies = zakonczone_miesiace(s)
    sma10 = mies.tail(10).mean()
    odch = (mies.iloc[-1] / sma10 - 1) * 100
    kiedy = f"zamknięcie {mies.index[-1]:%m.%Y}"
    if odch < 0:
        return "CZERWONY", odch, f"poniżej średniej 10-mies. - trend spadkowy ({kiedy})"
    if odch < TREND_BUFOR:
        return "ŻÓŁTY", odch, f"tuż nad średnią 10-mies. ({kiedy})"
    return "ZIELONY", odch, f"powyżej średniej 10-mies. - trend wzrostowy ({kiedy})"


def ocena_cape():
    dni = (date.today() - datetime.strptime(CAPE_DATA, "%Y-%m-%d").date()).days
    kom = f"wpis ręczny z {CAPE_DATA}"
    if dni > CAPE_STARE_DNI:
        kom += f" - NIEAKTUALNE ({dni} dni), zaktualizuj z multpl.com"
    return strefa(CAPE_RECZNIE, PROGI["CAPE"]), CAPE_RECZNIE, kom


# Grupy wg notatek Iwucia - każda odpowiada na inne pytanie.
GRUPY = {
    "Wycena (horyzont: dekada)": ["CAPE"],
    "Cykl i kredyt (wyprzedzające)": ["Krzywa 10Y-3M", "Spready HY"],
    "Potwierdzenie recesji (opóźniający)": ["Reguła Sahm"],
    "Rynek teraz (współbieżne)": ["VIX", "Trend S&P 500"],
}


def werdykt(oceny: dict) -> list:
    z = {k: v[0] for k, v in oceny.items()}
    linie = []
    if z.get("Reguła Sahm") == "CZERWONY":
        linie.append("Recesja potwierdzona danymi o zatrudnieniu.")
    nazwy_cyklu = GRUPY["Cykl i kredyt (wyprzedzające)"]
    cykl = [k for k in nazwy_cyklu if z.get(k) == "CZERWONY"]
    brak_cyklu = [k for k in nazwy_cyklu if z.get(k) == "BRAK"]
    if cykl:
        linie.append(f"Zapalnik w cyklu/kredycie: {', '.join(cykl)}. "
                     "Podwyższona czujność - przypomnij sobie plan na bessę.")
    elif brak_cyklu:
        # Brak danych to NIE to samo co brak zagrożenia.
        linie.append(f"Brak danych: {', '.join(brak_cyklu)} - "
                     "cyklu i kredytu nie da się ocenić.")
    else:
        linie.append("Cykl i kredyt bez zapalnika recesyjnego.")
    if z.get("CAPE") == "CZERWONY":
        linie.append("Wycena wysoka: niższe oczekiwane zwroty w dekadę. "
                     "O najbliższym kwartale nie mówi nic.")
    rynek = [k for k in GRUPY["Rynek teraz (współbieżne)"]
             if z.get(k) == "CZERWONY"]
    if rynek:
        linie.append("Rynek w strachu lub w trendzie spadkowym. Według planu: "
                     "rebalancing i wpłaty, bez sprzedaży pod wpływem emocji.")
    cz = sum(v == "CZERWONY" for v in z.values())
    zo = sum(v == "ŻÓŁTY" for v in z.values())
    br = sum(v == "BRAK" for v in z.values())
    linie.append(f"Czerwone: {cz}/{len(z)}, żółte: {zo}/{len(z)}"
                 + (f", brak danych: {br}/{len(z)}" if br else "")
                 + ". Barometr, nie zegarek - żadna strefa nie podaje daty.")
    return linie


# =====================================================================
# WYNIKI: tabela, wykres, historia
# =====================================================================

# =====================================================================
# POLSKA - kontekst dla obligacji i portfela w PLN (bez stref)
# =====================================================================

EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"


def pobierz_url(url: str, accept: str = None) -> str:
    naglowki = {"User-Agent": "Mozilla/5.0 (kompas-rynku)"}
    if accept:
        naglowki["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=naglowki), timeout=60) as odp:
        return odp.read().decode("utf-8", errors="replace")


def jsonstat_na_serie(obj: dict, wybor: list) -> pd.Series:
    """Odczyt formatu JSON-stat (Eurostat).

    Wszystkie liczby leżą w jednej płaskiej tablicy "value", a pozycję
    liczy się jak w tablicy wielowymiarowej: wymiary w kolejności obj["id"],
    ostatni zmienia się najszybciej. Dla każdego wymiaru poza czasem
    wybieramy jedną kategorię: po kodzie, a gdy kodu nie ma - po
    fragmencie etykiety (odporne na zmiany kodów, np. ECOICOP 2 w 2026).
    """
    ids, rozmiary = obj["id"], obj["size"]
    pozycja = {}
    for d in ids:
        if d == "time":
            continue
        kat = obj["dimension"][d]["category"]
        indeks, etykiety = kat["index"], kat.get("label", {})
        if isinstance(indeks, list):  # JSON-stat dopuszcza też listę kodów
            indeks = {k: i for i, k in enumerate(indeks)}
        if len(indeks) == 1:
            pozycja[d] = next(iter(indeks.values()))
            continue
        kod = None
        for prefiks, kody, fragmenty in wybor:
            if d.lower().startswith(prefiks):
                kod = next((k for k in kody if k in indeks), None) or next(
                    (k for k, l in etykiety.items() if any(f in l.lower() for f in fragmenty)), None)
                break
        if kod is None:
            raise ValueError(f"nie umiem wybrać kategorii w wymiarze '{d}'")
        pozycja[d] = indeks[kod]

    mnozniki, m = [], 1
    for r in reversed(rozmiary):
        mnozniki.insert(0, m)
        m *= r
    wartosci = obj.get("value", {})
    czas = obj["dimension"]["time"]["category"]["index"]
    if isinstance(czas, list):
        czas = {k: i for i, k in enumerate(czas)}
    wynik = {}
    for okres, poz in czas.items():
        plaski = sum((poz if d == "time" else pozycja[d]) * mn for d, mn in zip(ids, mnozniki))
        v = wartosci.get(str(plaski)) if isinstance(wartosci, dict) else (
            wartosci[plaski] if plaski < len(wartosci) else None)
        if v is not None:
            wynik[pd.Period(okres.replace("M", "-"), "M").to_timestamp()] = float(v)
    s = pd.Series(wynik, dtype=float).sort_index()
    if s.empty:
        raise ValueError("brak wartości dla wybranych kategorii")
    return s


def pobierz_eurostat(adresy: list, wybor: list):
    """Próbuje kolejnych zapytań, aż któreś zadziała. Zwraca (seria, zbiór)."""
    bledy = []
    for zapytanie in adresy:
        try:
            obj = json.loads(pobierz_url(EUROSTAT + zapytanie))
            return jsonstat_na_serie(obj, wybor), zapytanie.split("?")[0]
        except Exception as e:
            bledy.append(f"{zapytanie.split('?')[0]}: {e}")
    raise ValueError("; ".join(bledy))


def pobierz_inflacje_pl():
    # Od 2026 Eurostat publikuje HICP w klasyfikacji ECOICOP 2 (zbiór
    # prc_hicp_minr). Stary prc_hicp_manr jest zarchiwizowany - zostaje
    # jako zapas, ale kończy się na grudniu 2025.
    wybor = [("unit", ["RCH_A"], ["annual rate"]),
             ("coicop", ["TOTAL", "CP00"], ["all-items", "all items"]),
             ("geo", ["PL"], ["poland"])]
    return pobierz_eurostat([
        "prc_hicp_minr?format=JSON&geo=PL&unit=RCH_A&coicop18=TOTAL&sinceTimePeriod=2015-01",
        "prc_hicp_minr?format=JSON&geo=PL&unit=RCH_A&sinceTimePeriod=2015-01",
        "prc_hicp_manr?format=JSON&geo=PL&coicop=CP00&sinceTimePeriod=2015-01",
    ], wybor)


def pobierz_rentownosc_10l():
    # Średnie miesięczne rentowności 10-letnich obligacji (kryterium z Maastricht).
    wybor = [("int_rt", ["MCBY"], ["convergence"]), ("geo", ["PL"], ["poland"])]
    return pobierz_eurostat(["irt_lt_mcby_m?format=JSON&geo=PL&sinceTimePeriod=2015-01"], wybor)


def pobierz_kurs_nbp(waluta: str) -> pd.Series:
    """Średnie kursy NBP (tabela A). API oddaje maks. 367 dni na zapytanie,
    więc pobieramy w kawałkach po 360 dni."""
    koniec = date.today()
    a = koniec - timedelta(days=365 * LAT_NA_WYKRESIE + 31)
    punkty = {}
    while a <= koniec:
        b = min(a + timedelta(days=360), koniec)
        url = (f"https://api.nbp.pl/api/exchangerates/rates/a/{waluta}/"
               f"{a:%Y-%m-%d}/{b:%Y-%m-%d}/?format=json")
        try:
            for r in json.loads(pobierz_url(url, "application/json"))["rates"]:
                punkty[pd.Timestamp(r["effectiveDate"])] = float(r["mid"])
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = brak notowań w tym przedziale (np. weekend)
                raise
        a = b + timedelta(days=1)
    if not punkty:
        raise ValueError("NBP nie zwrócił kursów")
    return pd.Series(punkty).sort_index()


def stopa_referencyjna() -> tuple:
    """Historia stopy referencyjnej z listy STOPA_REF_ZMIANY + próba
    sprawdzenia aktualnej stopy w pliku XML NBP. Zwraca (seria, uwaga)."""
    zmiany = dict(STOPA_REF_ZMIANY)
    uwaga = "wg listy STOPA_REF_ZMIANY w skrypcie"
    try:
        xml = pobierz_url("https://static.nbp.pl/dane/stopy/stopy_procentowe.xml")
        znacznik = re.search(r'<pozycja[^>]*id="ref"[^>]*>', xml)
        stopa = re.search(r'oprocentowanie="([\d,\.]+)"', znacznik.group(0))
        od = re.search(r'obowiazuje_od="(\d{4}-\d{2}-\d{2})"', znacznik.group(0)) or \
            re.search(r'obowiazuje_od="(\d{4}-\d{2}-\d{2})"', xml)
        nbp = (od.group(1), float(stopa.group(1).replace(",", ".")))
        ostatnia = sorted(zmiany.items())[-1]
        if nbp[1] != ostatnia[1] or nbp[0] > ostatnia[0]:
            zmiany[nbp[0]] = nbp[1]
            uwaga = (f"aktualna stopa pobrana z NBP ({nbp[1]:.2f}% od {nbp[0]}) - "
                     "dopisz ją do STOPA_REF_ZMIANY")
        else:
            uwaga = "zgodna z NBP"
    except Exception:
        pass  # brak dostępu do NBP: zostaje lista z konfiguracji
    s = pd.Series({pd.Timestamp(d): v for d, v in zmiany.items()}).sort_index()
    return s, uwaga


def zmiana_proc(s: pd.Series, miesiace: int) -> float:
    przed = s[s.index <= s.index[-1] - pd.DateOffset(months=miesiace)]
    return (s.iloc[-1] / przed.iloc[-1] - 1) * 100 if len(przed) else float("nan")


def analiza_polska(pl: dict) -> list:
    """Same fakty i proste różnice - bez stref i bez prognoz."""
    linie = []
    inf, ref, y10 = pl.get("inflacja"), pl.get("stopa"), pl.get("y10")
    if inf is not None:
        poza = "" if CEL_INFLACYJNY[0] <= inf.iloc[-1] <= CEL_INFLACYJNY[1] else ", poza pasmem celu"
        linie.append(f"Inflacja HICP r/r: {inf.iloc[-1]:.1f}% (dane za {inf.index[-1]:%m.%Y})"
                     f"; cel NBP 2,5% ± 1 pp{poza}.")
    if ref is not None:
        linie.append(f"Stopa referencyjna NBP: {ref.iloc[-1]:.2f}% "
                     f"(od {ref.index[-1]:%d.%m.%Y}; {pl.get('stopa_uwaga', '')}).")
    if inf is not None and ref is not None:
        r = ref.iloc[-1] - inf.iloc[-1]
        opis = "polityka pieniężna restrykcyjna" if r > 0 else "polityka pieniężna luźna"
        linie.append(f"Realna stopa NBP (stopa ref. minus inflacja): {r:+.1f} pp, {opis}.")
    if y10 is not None:
        t = f"Rentowność 10-letnich obligacji skarbowych: {y10.iloc[-1]:.2f}% (średnia za {y10.index[-1]:%m.%Y})"
        if inf is not None:
            t += f", realnie {y10.iloc[-1] - inf.iloc[-1]:+.1f} pp ponad inflację"
        linie.append(t + ".")
    for w in ("EUR", "USD"):
        s = pl.get(w)
        if s is not None:
            linie.append(f"{w}/PLN: {s.iloc[-1]:.4f} (zmiana 3 mies. {zmiana_proc(s, 3):+.1f}%, "
                         f"12 mies. {zmiana_proc(s, 12):+.1f}%).")
    if pl.get("EUR") is not None or pl.get("USD") is not None:
        linie.append("Wyższy kurs, czyli słabszy złoty, podnosi wartość VWCE liczoną w PLN.")
    for nazwa, blad in pl.get("bledy", {}).items():
        linie.append(f"Brak danych: {nazwa} ({blad[:120]}).")
    return linie


def dane_polska_demo() -> dict:
    rng = np.random.default_rng(7)
    mies = pd.date_range("2015-01-01", date.today(), freq="MS")
    rok = mies.year + mies.month / 12
    inf = 1.5 + 16 * np.exp(-((rok - 2023.1) / 0.6) ** 2) + 0.8 * (rok > 2026.4) + rng.normal(0, 0.2, len(mies))
    y10 = 3 + 3.5 * np.exp(-((rok - 2022.9) / 1.0) ** 2) + 2 * (rok > 2022) + rng.normal(0, 0.1, len(mies))
    dni = pd.bdate_range(date.today() - timedelta(days=365 * LAT_NA_WYKRESIE + 31), date.today())
    eur = 4.4 + np.cumsum(rng.normal(0, 0.006, len(dni)))
    usd = 4.0 + np.cumsum(rng.normal(0, 0.008, len(dni)))
    s = pd.Series({pd.Timestamp(d): v for d, v in STOPA_REF_ZMIANY}).sort_index()
    return {"inflacja": pd.Series(inf, index=mies), "y10": pd.Series(y10, index=mies),
            "EUR": pd.Series(eur, index=dni), "USD": pd.Series(usd, index=dni),
            "stopa": s, "stopa_uwaga": "wg listy STOPA_REF_ZMIANY w skrypcie", "bledy": {}}


def pobierz_polska() -> dict:
    pl, bledy = {}, {}
    for nazwa, klucz, funkcja in [
        ("inflacja HICP (Eurostat)", "inflacja", lambda: pobierz_inflacje_pl()[0]),
        ("rentowność 10-latek (Eurostat)", "y10", lambda: pobierz_rentownosc_10l()[0]),
        ("kurs EUR/PLN (NBP)", "EUR", lambda: pobierz_kurs_nbp("eur")),
        ("kurs USD/PLN (NBP)", "USD", lambda: pobierz_kurs_nbp("usd")),
    ]:
        try:
            pl[klucz] = funkcja()
            print(f"  pobrano {nazwa}: ostatni odczyt {pl[klucz].index[-1]:%Y-%m-%d}")
        except Exception as e:  # polskie dane są kontekstem - ich brak nie blokuje raportu
            bledy[nazwa] = str(e)
            print(f"  BŁĄD pobierania {nazwa}: {e}")
    pl["stopa"], pl["stopa_uwaga"] = stopa_referencyjna()
    pl["bledy"] = bledy
    return pl


def przygotuj_polska(pl: dict) -> dict:
    """Punkty do dwóch wykresów: stopy i inflacja oraz kursy walut."""
    od = pd.Timestamp(date.today()) - pd.DateOffset(years=LAT_NA_WYKRESIE)

    def pkt(s):
        s = s[s.index >= od].dropna()
        return [[int(t.timestamp() * 1000), round(float(v), 4)] for t, v in s.items()]

    stopy = []
    if pl.get("inflacja") is not None:
        stopy.append({"nazwa": "inflacja HICP r/r", "pkt": pkt(pl["inflacja"]), "kolor": "--tusz"})
    if pl.get("stopa") is not None:
        s = pl["stopa"]
        # punkt startowy wykresu i "dziś", żeby schodki sięgały krawędzi
        przed = s[s.index <= od]
        s = pd.concat([pd.Series({od: przed.iloc[-1]}) if len(przed) else pd.Series(dtype=float),
                       s[s.index > od], pd.Series({pd.Timestamp(date.today()): s.iloc[-1]})])
        stopy.append({"nazwa": "stopa referencyjna NBP", "pkt": pkt(s), "kolor": "--seria2", "schodki": True})
    if pl.get("y10") is not None:
        stopy.append({"nazwa": "rentowność 10-latek", "pkt": pkt(pl["y10"]), "kolor": "--tusz3", "kreski": True})
    kursy = []
    for w, kolor in (("EUR", "--tusz"), ("USD", "--seria2")):
        if pl.get(w) is not None:
            kursy.append({"nazwa": f"{w}/PLN", "pkt": pkt(pl[w].resample("W").last()), "kolor": kolor})
    return {"linie": analiza_polska(pl), "stopy": stopy, "kursy": kursy,
            "pasmo": list(CEL_INFLACYJNY)}


def historia_polska(pl: dict) -> dict:
    w = {}
    for klucz, kolumna in (("inflacja", "PL inflacja HICP [%]"), ("stopa", "PL stopa ref. [%]"),
                           ("y10", "PL rentowność 10L [%]"), ("EUR", "EUR/PLN"), ("USD", "USD/PLN")):
        s = pl.get(klucz)
        w[kolumna] = round(float(s.iloc[-1]), 4) if s is not None else None
    return w


def formatuj(nazwa: str, wart: float) -> str:
    if pd.isna(wart):
        return "brak"
    if nazwa == "Trend S&P 500":
        return f"{wart:+.1f}%"
    if nazwa in ("CAPE", "VIX"):
        return f"{wart:.1f}"
    return f"{wart:.2f}"


def przygotuj_serie(dane: dict) -> dict:
    """Zamienia serie na listy punktów [czas w ms, wartość] dla wykresów.

    Serie dzienne skracamy do tygodniowych (ostatni odczyt tygodnia):
    plik HTML jest kilka razy lżejszy, a kształt wykresu ten sam.
    Czas w milisekundach, bo tak liczy daty JavaScript.
    """
    od = pd.Timestamp(date.today()) - pd.DateOffset(years=LAT_NA_WYKRESIE)

    def pkt(s):
        s = s[s.index >= od].dropna()
        return [[int(t.timestamp() * 1000), round(float(v), 4)] for t, v in s.items()]

    def tyg(s):
        return s.resample("W").last()

    serie = {}
    if "T10Y3M" in dane:
        serie["Krzywa 10Y-3M"] = {"pkt": pkt(tyg(dane["T10Y3M"])), "zero": True}
    if "BAMLH0A0HYM2" in dane:
        serie["Spready HY"] = {"pkt": pkt(tyg(dane["BAMLH0A0HYM2"])), "progi": PROGI["HY_poziom"]}
    if "SAHMREALTIME" in dane:
        serie["Reguła Sahm"] = {"pkt": pkt(dane["SAHMREALTIME"]), "progi": PROGI["Sahm"]}
    if "VIXCLS" in dane:
        serie["VIX"] = {"pkt": pkt(tyg(dane["VIXCLS"])), "progi": PROGI["VIX"]}
    if "SP500" in dane:
        sp = dane["SP500"]
        sma = zakonczone_miesiace(sp).rolling(10).mean()
        serie["Trend S&P 500"] = {"pkt": pkt(tyg(sp)), "pkt2": pkt(sma)}
    return serie


def zapisz_html(dane: dict, oceny: dict, linie: list, demo: bool, pl: dict) -> Path:
    """Wkłada dane jako JSON do szablonu HTML. Cały wygląd i wykresy
    robi przeglądarka (Chart.js), Python tylko liczy i dostarcza liczby."""
    dane_raportu = {
        "data": f"{date.today():%Y-%m-%d}",
        "demo": demo,
        "grupy": [[g, n] for g, n in GRUPY.items()],
        "oceny": {k: {"strefa": z, "wartosc": formatuj(k, w), "komentarz": kom}
                  for k, (z, w, kom) in oceny.items()},
        "werdykt": linie,
        "serie": przygotuj_serie(dane),
        "polska": przygotuj_polska(pl),
        "cape": {"wartosc": CAPE_RECZNIE, "progi": list(PROGI["CAPE"]),
                 "data": CAPE_DATA},
    }
    # "</" w JSON mogłoby przedwcześnie zamknąć znacznik <script>.
    js = json.dumps(dane_raportu, ensure_ascii=False).replace("</", "<\\/")
    html = SZABLON_HTML.replace("__DANE__", js)
    plik = FOLDER / f"kompas_{date.today():%Y-%m-%d}.html"
    plik.write_text(html, encoding="utf-8")
    # Stała nazwa ostatniego raportu - można ją dodać do zakładek.
    (FOLDER / "kompas_najnowszy.html").write_text(html, encoding="utf-8")
    return plik


SZABLON_HTML = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kompas rynku</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@500;600&display=swap" rel="stylesheet">
<style>
:root{--tlo:#eef0f2;--karta:#fff;--tusz:#1c2024;--tusz2:#545c64;--tusz3:#7d868e;--linia:#d5dade;
--zielony:#2f8a4a;--zolty:#c98a00;--czerwony:#c4372c;--szary:#8a929a;--seria2:#2f6fb5}
@media (prefers-color-scheme:dark){:root{--tlo:#121518;--karta:#1b1f23;--tusz:#e4e7ea;--tusz2:#a8b0b7;
--tusz3:#78818a;--linia:#2c3238;--seria2:#6aa3e0}}
*{box-sizing:border-box}
body{margin:0;background:var(--tlo);color:var(--tusz);font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1080px;margin:0 auto;padding:28px 20px 48px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font:600 28px/1.15 "IBM Plex Sans Condensed",sans-serif;margin:0}
.meta{color:var(--tusz2);font-size:13px}
.demo{color:var(--czerwony);font-weight:600}
.panel{background:#1a1f24;border-radius:10px;padding:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}
.lampka{border:1px solid #2e353b;border-radius:4px;padding:10px 12px;background:#14181c;color:#7c858d;font:600 15px/1.25 "IBM Plex Sans Condensed",sans-serif}
.lampka small{display:block;font-weight:500;font-size:12.5px;margin-top:3px}
.lampka.ZIELONY{color:#7ee09a;border-color:#2f8a4a;background:#11251a}
.lampka.ŻÓŁTY{color:#ffcc55;border-color:#c98a00;background:#2a2008}
.lampka.CZERWONY{color:#ff8478;border-color:#c4372c;background:#2e1311}
.werdykt{margin:18px 0 4px;padding:0;list-style:none;max-width:76ch}
.werdykt li{padding-left:16px;position:relative;margin:5px 0}
.werdykt li::before{content:"";position:absolute;left:2px;top:.62em;width:6px;height:6px;border-radius:50%;background:var(--tusz3)}
h2{font:600 16px/1.3 "IBM Plex Sans Condensed",sans-serif;color:var(--tusz2);margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--linia)}
.siatka{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.karta{background:var(--karta);border:1px solid var(--linia);border-radius:8px;padding:14px 16px}
.glowa{display:flex;justify-content:space-between;align-items:center;gap:10px}
.nazwa{color:var(--tusz2);font-size:14px}
.strefa{font:600 12.5px/1 "IBM Plex Sans Condensed",sans-serif;padding:5px 8px;border-radius:3px;color:#fff;background:var(--szary)}
.s-ZIELONY{background:var(--zielony)}.s-ŻÓŁTY{background:var(--zolty);color:#1f1600}.s-CZERWONY{background:var(--czerwony)}
.wartosc{font:600 30px/1.15 "IBM Plex Sans Condensed",sans-serif;margin-top:6px}
.kom{color:var(--tusz3);font-size:13px;min-height:2.6em}
.wykres{position:relative;height:150px;margin-top:6px}
.miara{position:relative;height:16px;border-radius:3px;overflow:hidden;display:flex;margin-top:16px}
.skala{position:relative;height:16px;font-size:11.5px;color:var(--tusz3);margin-top:4px}
.skala span{position:absolute;transform:translateX(-50%)}
.brakjs{color:var(--tusz3);font-size:13px;margin-top:10px}
footer{margin-top:34px;color:var(--tusz3);font-size:13px;max-width:80ch}
</style>
</head>
<body>
<main>
<header><h1>Kompas rynku</h1><div class="meta" id="meta"></div></header>
<section class="panel" id="panel" aria-label="Stan wskaźników"></section>
<ul class="werdykt" id="werdykt"></ul>
<div id="grupy"></div>
<div id="polska"></div>
<footer>Kompas pokazuje stan rynku według reguł ustalonych z góry. Nie podaje daty spadków ani wzrostów.
Progi stref zmieniasz w słowniku PROGI w skrypcie. Każde uruchomienie dopisuje odczyt do pliku kompas_historia.csv.</footer>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const D = __DANE__;
const ETYK = {ZIELONY:"zielony", "ŻÓŁTY":"żółty", CZERWONY:"czerwony", BRAK:"brak danych"};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dzien = v => {const d = new Date(v); return String(d.getDate()).padStart(2,"0")+"."+String(d.getMonth()+1).padStart(2,"0")+"."+d.getFullYear();};

document.getElementById("meta").innerHTML = "Stan na " + D.data + ". Dane: FRED, CAPE wpisane ręcznie " + D.cape.data + "." +
  (D.demo ? ' <span class="demo">Dane demo, nie prawdziwe.</span>' : "");
const kolejnosc = D.grupy.flatMap(g => g[1]);
document.getElementById("panel").innerHTML = kolejnosc.map(k => {
  const o = D.oceny[k];
  return '<div class="lampka ' + o.strefa + '">' + k + "<small>" + o.wartosc + ", " + ETYK[o.strefa] + "</small></div>";
}).join("");
document.getElementById("werdykt").innerHTML = D.werdykt.map(l => "<li>" + l + "</li>").join("");

const ID = {}; let n = 0, h = "";
const proc = x => Math.min(100, Math.max(0, x / 50 * 100));
D.grupy.forEach(([grupa, nazwy]) => {
  h += "<h2>" + grupa + '</h2><div class="siatka">';
  nazwy.forEach(k => {
    const o = D.oceny[k];
    h += '<div class="karta"><div class="glowa"><span class="nazwa">' + k + '</span><span class="strefa s-' + o.strefa + '">' + ETYK[o.strefa] + "</span></div>" +
         '<div class="wartosc">' + o.wartosc + '</div><div class="kom">' + o.komentarz + "</div>";
    if (k === "CAPE") {
      const p = D.cape.progi;
      h += '<div class="miara"><div style="width:' + proc(p[0]) + '%;background:var(--zielony);opacity:.35"></div>' +
           '<div style="width:' + (proc(p[1]) - proc(p[0])) + '%;background:var(--zolty);opacity:.4"></div>' +
           '<div style="flex:1;background:var(--czerwony);opacity:.35"></div>' +
           '<div style="position:absolute;left:' + proc(D.cape.wartosc) + '%;top:0;bottom:0;width:3px;background:var(--tusz)"></div>' +
           '<div style="position:absolute;left:' + proc(44.2) + '%;top:0;bottom:0;border-left:1px dashed var(--tusz2)"></div></div>' +
           '<div class="skala"><span style="left:2%">0</span><span style="left:' + proc(p[0]) + '%">' + p[0] + '</span><span style="left:' + proc(p[1]) + '%">' + p[1] +
           '</span><span style="left:' + proc(44.2) + '%">rekord 1999</span></div>';
    } else if (D.serie[k]) {
      ID[k] = "w" + (n++);
      h += '<div class="wykres"><canvas id="' + ID[k] + '" role="img" aria-label="Wykres: ' + k + ' z ostatnich lat na tle stref"></canvas></div>';
    }
    h += "</div>";
  });
  h += "</div>";
});
document.getElementById("grupy").innerHTML = h;

// Polska: fakty bez stref + dwa wykresy
const P = D.polska;
let hp = '<h2>Polska: stopy, inflacja, złoty</h2><div class="karta"><ul class="werdykt" style="margin:0">' +
  P.linie.map(l => "<li>" + l + "</li>").join("") + "</ul>" +
  '<p class="kom" style="margin:10px 0 0;min-height:0">Obligacje detaliczne: ROR i DOR idą za stopą referencyjną NBP; ' +
  "OTS i TOS mają stałe oprocentowanie ustalone przy zakupie; COI, EDO i ROD zarabiają inflację CPI z GUS (z opóźnieniem) plus marżę. " +
  "Inflacja na wykresie to HICP z Eurostatu, zwykle bliska CPI z GUS.</p></div>" + '<div class="siatka" style="margin-top:14px">';
[["stopy", "Stopy i inflacja [%], zielone pole: cel NBP", "pl0"], ["kursy", "Kursy średnie NBP [zł]", "pl1"]].forEach(([k, tytul, id]) => {
  if (P[k].length) hp += '<div class="karta"><div class="glowa"><span class="nazwa">' + tytul + '</span></div>' +
    '<div class="wykres" style="height:230px"><canvas id="' + id + '" role="img" aria-label="Wykres: ' + tytul + '"></canvas></div></div>';
});
document.getElementById("polska").innerHTML = hp + "</div>";

if (!window.Chart) {
  document.querySelectorAll(".wykres").forEach(el => el.outerHTML = '<div class="brakjs">Wykresy wymagają internetu (biblioteka Chart.js).</div>');
} else {
  const pasy = {id: "pasy", beforeDatasetsDraw(c, a, o) {
    const {ctx, chartArea: ca, scales: {y}} = c;
    if (o.progi) {
      [[y.min, o.progi[0], "rgba(47,138,74,0.11)"], [o.progi[0], o.progi[1], "rgba(201,138,0,0.15)"], [o.progi[1], y.max, "rgba(196,55,44,0.11)"]]
        .forEach(([lo, hi, kol]) => {
          const g = y.getPixelForValue(Math.min(hi, y.max)), d = y.getPixelForValue(Math.max(lo, y.min));
          if (d > g) { ctx.fillStyle = kol; ctx.fillRect(ca.left, g, ca.right - ca.left, d - g); }
        });
    }
    if (o.pasmo) {
      const g = y.getPixelForValue(Math.min(o.pasmo[1], y.max)), d = y.getPixelForValue(Math.max(o.pasmo[0], y.min));
      if (d > g) { ctx.fillStyle = "rgba(47,138,74,0.12)"; ctx.fillRect(ca.left, g, ca.right - ca.left, d - g); }
    }
    if (o.zero) {
      const p = y.getPixelForValue(0);
      ctx.save(); ctx.strokeStyle = "#c4372c"; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(ca.left, p); ctx.lineTo(ca.right, p); ctx.stroke(); ctx.restore();
    }
  }};
  Object.keys(ID).forEach(k => {
    const s = D.serie[k], ys = s.pkt.map(p => p[1]);
    let mn, mx;
    if (s.progi) { mn = Math.min(0, ...ys); mx = Math.max(Math.max(...ys) * 1.05, s.progi[1] * 1.25); }
    const ds = [{label: k, data: s.pkt.map(p => ({x: p[0], y: p[1]})), borderColor: css("--tusz"), borderWidth: 1.6, pointRadius: 0, tension: 0.2}];
    if (s.pkt2) ds.push({label: "średnia 10-mies.", data: s.pkt2.map(p => ({x: p[0], y: p[1]})), borderColor: css("--seria2"), borderDash: [5, 4], borderWidth: 2, pointRadius: 0});
    new Chart(document.getElementById(ID[k]), {type: "line", data: {datasets: ds}, plugins: [pasy],
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "nearest", axis: "x", intersect: false},
        plugins: {legend: {display: false}, pasy: {progi: s.progi, zero: s.zero},
          tooltip: {callbacks: {title: it => dzien(it[0].parsed.x), label: c => c.dataset.label + ": " + c.parsed.y.toFixed(2)}}},
        scales: {x: {type: "linear", min: s.pkt[0][0], max: s.pkt[s.pkt.length - 1][0],
                    afterBuildTicks: ax => { ax.ticks = []; for (let r = new Date(ax.min).getFullYear() + 1; r <= new Date(ax.max).getFullYear(); r++) ax.ticks.push({value: Date.UTC(r, 0, 1)}); },
                    ticks: {color: css("--tusz3"), callback: v => new Date(v).getFullYear()}, grid: {display: false}},
                 y: {min: mn, max: mx, ticks: {color: css("--tusz3"), maxTicksLimit: 5}, grid: {color: css("--linia")}}}}});
  });
  const latka = ax => { ax.ticks = []; for (let r = new Date(ax.min).getFullYear() + 1; r <= new Date(ax.max).getFullYear(); r++) ax.ticks.push({value: Date.UTC(r, 0, 1)}); };
  [["stopy", "pl0"], ["kursy", "pl1"]].forEach(([k, id]) => {
    if (!P[k].length) return;
    const ds = P[k].map(s => ({label: s.nazwa, data: s.pkt.map(p => ({x: p[0], y: p[1]})), borderColor: css(s.kolor),
      backgroundColor: css(s.kolor), borderWidth: 2, pointRadius: 0, stepped: !!s.schodki,
      borderDash: s.kreski ? [5, 4] : [], tension: s.schodki ? 0 : 0.2}));
    const xs = ds.flatMap(d => d.data.map(p => p.x));
    new Chart(document.getElementById(id), {type: "line", data: {datasets: ds}, plugins: [pasy],
      options: {responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "nearest", axis: "x", intersect: false},
        plugins: {legend: {position: "bottom", labels: {color: css("--tusz2"), boxWidth: 16, boxHeight: 2, font: {size: 12}}},
          pasy: k === "stopy" ? {pasmo: P.pasmo} : {},
          tooltip: {callbacks: {title: it => dzien(it[0].parsed.x), label: c => c.dataset.label + ": " + c.parsed.y.toFixed(k === "kursy" ? 4 : 2)}}},
        scales: {x: {type: "linear", min: Math.min(...xs), max: Math.max(...xs), afterBuildTicks: latka,
                     ticks: {color: css("--tusz3"), callback: v => new Date(v).getFullYear()}, grid: {display: false}},
                 y: {ticks: {color: css("--tusz3"), maxTicksLimit: 6}, grid: {color: css("--linia")}}}}});
  });
}
</script>
</body>
</html>
"""


def dopisz_historie(oceny: dict, linie: list, demo: bool, pl: dict):
    wiersz = {"data": f"{date.today():%Y-%m-%d}", "demo": demo}
    for k, (zs, wart, _) in oceny.items():
        wiersz[f"{k} [wartość]"] = round(float(wart), 3) if zs != "BRAK" else None
        wiersz[f"{k} [strefa]"] = zs
    wiersz.update(historia_polska(pl))
    wiersz["werdykt"] = " | ".join(linie)
    plik = FOLDER / "kompas_historia.csv"
    nowy = pd.DataFrame([wiersz])
    # Wczytujemy i zapisujemy całość zamiast dopisywać: gdy dojdą nowe
    # kolumny (jak teraz polskie dane), stare wiersze dostaną puste pola,
    # a kolumny się nie rozjadą.
    if plik.exists():
        nowy = pd.concat([pd.read_csv(plik, encoding="utf-8-sig"), nowy], ignore_index=True)
    nowy.to_csv(plik, index=False, encoding="utf-8-sig")
    return plik


def main():
    parser = argparse.ArgumentParser(description="Kompas rynku")
    parser.add_argument("--demo", action="store_true",
                        help="dane syntetyczne zamiast FRED")
    demo = parser.parse_args().demo
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # polskie znaki w konsoli Windows
    except Exception:
        pass
    FOLDER.mkdir(exist_ok=True)

    if demo:
        dane = dane_demo()
        pl = dane_polska_demo()
    else:
        dane = {}
        for seria in ["T10Y3M", "BAMLH0A0HYM2", "SAHMREALTIME", "VIXCLS", "SP500"]:
            try:
                dane[seria] = pobierz_fred(seria)
                print(f"  pobrano {seria}: ostatni odczyt {dane[seria].index[-1]:%Y-%m-%d}")
            except Exception as e:  # jedna niedostępna seria nie zatrzymuje reszty
                print(f"  BŁĄD pobierania {seria}: {e}")
        if not dane:
            # Nic się nie pobrało: kończymy z błędem, żeby nie nadpisać
            # dobrego raportu pustym. GitHub oznaczy przebieg na czerwono.
            print("\nNie udało się pobrać żadnej serii z FRED - raport nie został zapisany.")
            sys.exit(1)
        pl = pobierz_polska()

    funkcje = {
        "CAPE": (None, lambda _: ocena_cape()),
        "Krzywa 10Y-3M": ("T10Y3M", ocena_krzywej),
        "Spready HY": ("BAMLH0A0HYM2", ocena_hy),
        "Reguła Sahm": ("SAHMREALTIME", ocena_sahm),
        "VIX": ("VIXCLS", ocena_vix),
        "Trend S&P 500": ("SP500", ocena_trendu),
    }
    oceny = {}
    for nazwa, (seria, f) in funkcje.items():
        if seria is not None and seria not in dane:
            oceny[nazwa] = ("BRAK", float("nan"), "brak danych")
        else:
            oceny[nazwa] = f(dane.get(seria))

    print(f"\nKOMPAS RYNKU {date.today():%Y-%m-%d}" + ("  [DEMO]" if demo else ""))
    for grupa, nazwy in GRUPY.items():
        print(f"\n{grupa}")
        for n in nazwy:
            zs, wart, kom = oceny[n]
            print(f"  {n:<15} {wart:>8.2f}  {zs:<9} {kom}")

    linie = werdykt(oceny)
    print("\nPODSUMOWANIE")
    for l in linie:
        print(f"  - {l}")

    print("\nPOLSKA (kontekst, bez stref)")
    for l in analiza_polska(pl):
        print(f"  - {l}")

    raport = zapisz_html(dane, oceny, linie, demo, pl)
    csv = dopisz_historie(oceny, linie, demo, pl)
    print(f"\nRaport:   {raport}\n          (ostatni zawsze też jako kompas_najnowszy.html)"
          f"\nHistoria: {csv}")


if __name__ == "__main__":
    main()
