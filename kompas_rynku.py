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
import sys
import urllib.request
from datetime import date, datetime
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
    """Pobiera serię z FRED jako CSV (bez klucza API).

    FRED zwraca 2 kolumny: data i wartość. Nazwa kolumny daty bywała
    różna ("DATE" lub "observation_date"), dlatego bierzemy kolumny po
    pozycji, a nie po nazwie. Braki danych FRED oznacza kropką ".".
    """
    url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id={seria}&cosd={DANE_OD}")
    zapytanie = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (kompas-rynku)"})
    with urllib.request.urlopen(zapytanie, timeout=30) as odp:
        tekst = odp.read().decode("utf-8")
    return csv_na_serie(tekst, seria)


def csv_na_serie(tekst: str, nazwa: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(tekst), na_values=".")
    daty = pd.to_datetime(df.iloc[:, 0])
    wartosci = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    s = pd.Series(wartosci.values, index=daty, name=nazwa).dropna()
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
    cykl = [k for k in GRUPY["Cykl i kredyt (wyprzedzające)"]
            if z.get(k) == "CZERWONY"]
    if cykl:
        linie.append(f"Zapalnik w cyklu/kredycie: {', '.join(cykl)}. "
                     "Podwyższona czujność - przypomnij sobie plan na bessę.")
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
    linie.append(f"Czerwone: {cz}/{len(z)}, żółte: {zo}/{len(z)}. "
                 "Barometr, nie zegarek - żadna strefa nie podaje daty.")
    return linie


# =====================================================================
# WYNIKI: tabela, wykres, historia
# =====================================================================

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


def zapisz_html(dane: dict, oceny: dict, linie: list, demo: bool) -> Path:
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
}
</script>
</body>
</html>
"""


def dopisz_historie(oceny: dict, linie: list, demo: bool):
    wiersz = {"data": f"{date.today():%Y-%m-%d}", "demo": demo}
    for k, (zs, wart, _) in oceny.items():
        wiersz[f"{k} [wartość]"] = round(float(wart), 3) if zs != "BRAK" else None
        wiersz[f"{k} [strefa]"] = zs
    wiersz["werdykt"] = " | ".join(linie)
    plik = FOLDER / "kompas_historia.csv"
    pd.DataFrame([wiersz]).to_csv(plik, mode="a", index=False,
                                  header=not plik.exists(), encoding="utf-8-sig")
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
    else:
        dane = {}
        for seria in ["T10Y3M", "BAMLH0A0HYM2", "SAHMREALTIME", "VIXCLS", "SP500"]:
            try:
                dane[seria] = pobierz_fred(seria)
                print(f"  pobrano {seria}: ostatni odczyt {dane[seria].index[-1]:%Y-%m-%d}")
            except Exception as e:  # jedna niedostępna seria nie zatrzymuje reszty
                print(f"  BŁĄD pobierania {seria}: {e}")

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

    raport = zapisz_html(dane, oceny, linie, demo)
    csv = dopisz_historie(oceny, linie, demo)
    print(f"\nRaport:   {raport}\n          (ostatni zawsze też jako kompas_najnowszy.html)"
          f"\nHistoria: {csv}")


if __name__ == "__main__":
    main()
