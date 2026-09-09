# FactReactor — Website

Statische Website zum YouTube-Kanal [FactReactor](https://www.youtube.com/channel/UCnWVqsL-mppKqIWIJypERiQ).
Für jedes veröffentlichte Short entsteht automatisch eine eigene Unterseite mit
eingebettetem Video, Beschreibung und strukturierten Metadaten. Die Startseite
listet alle Videos chronologisch, neu nach alt.

## Wie es läuft

```
YouTube publiziert
        ↓
GitHub Actions (stündlich)
   liest den öffentlichen Kanal-Feed
        ↓
   data/videos.json  ← Archiv, wächst, verliert nie etwas
        ↓
   _site/  →  Pages-Artefakt  →  Deploy
```

Der Kanal-Feed liefert immer nur die **neuesten 15 Videos**. Deshalb wird jeder
gesehene Eintrag in `data/videos.json` festgehalten und dort nie gelöscht — das
Archiv ist die Wahrheit, der Feed nur der Zulieferer.

Deployt wird ausschliesslich der Inhalt von `_site/`, hochgeladen als
Pages-Artefakt. Andere Dateien im Repo werden nie ausgeliefert.

## Was auf jeder Videoseite steht

- `<iframe>` auf `youtube-nocookie.com` — keine Tracking-Cookies vor dem Klick
- **JSON-LD `VideoObject`** mit `name`, `description`, `uploadDate`,
  `thumbnailUrl`, `embedUrl`, `contentUrl` — macht die Seite für Googles
  Video-Ergebnisse kandidatenfähig
- Open-Graph- und Twitter-Card-Tags für Link-Vorschauen
- **Self-Canonical.** Bewusst nicht auf YouTube zeigend: ein Canonical auf
  youtube.com würde Google anweisen, dort statt hier zu indexieren, und die
  Seite käme nie in die Suchergebnisse.

## Einrichtung

1. Repo anlegen (öffentlich) und diesen Inhalt pushen.
2. **Settings → Pages → Source: „GitHub Actions"**. Nicht „Deploy from a branch".
3. Workflow einmal von Hand starten: **Actions → Build and deploy site → Run workflow**.

Danach läuft er stündlich von selbst.

### Eigene Domain

1. In **Settings → Secrets and variables → Actions → Variables** eine Variable
   `BASE_URL` anlegen, Wert z. B. `https://factreactor.com`.
2. Datei `CNAME` mit dem blanken Hostnamen (`factreactor.com`) im Repo-Root
   anlegen und in `scripts/build_site.py` unter `main()` nach `_site/` kopieren
   lassen — oder die Domain einfach unter **Settings → Pages → Custom domain**
   eintragen, dann verwaltet GitHub die Datei selbst.

Ohne `BASE_URL` wird die Pages-URL aus `GITHUB_REPOSITORY` abgeleitet. Ohne
absolute Basis-URL entfallen `sitemap.xml`, `robots.txt` und die Canonical-Tags —
die Seite funktioniert, ist aber für Suchmaschinen schwächer.

## Lokal bauen

```bash
python3 scripts/build_site.py          # holt den echten Feed
open _site/index.html
```

Ohne Netzzugang gegen eine gespeicherte Feed-Datei:

```bash
FEED_FILE=feed.xml GITHUB_REPOSITORY=owner/repo python3 scripts/build_site.py
```

Keine Abhängigkeiten ausser der Python-Standardbibliothek.

## Bekannte Grenzen

- **Cron in GitHub Actions ist unpünktlich.** Läufe verzögern sich regelmässig um
  5–20 Minuten, unter Last fallen einzelne aus. Deshalb stündlich statt einmal
  täglich: ein ausgefallener Lauf wird vom nächsten aufgeholt.
- **Die Videolänge fehlt.** Der Feed liefert sie nicht, deshalb steht `duration`
  nicht im `VideoObject`. Wer sie will, trägt sie pro Video von Hand in
  `data/videos.json` ein (ISO 8601, z. B. `"duration": "PT51S"`); der Generator
  übernimmt sie dann.
- **Titeländerungen ändern den Slug nicht.** Einmal vergebene URLs bleiben
  stabil, damit geteilte Links nicht brechen. Der angezeigte Titel wird bei jedem
  Lauf aus dem Feed aufgefrischt.

## Themen

Die Zuordnung steht in `data/topics.json` — Video-ID zu Thema:

```json
{
  "TEipugAb-GE": "Anatomy",
  "cbUPlcHJSFs": "Anatomy"
}
```

Auf der Startseite entstehen daraus Filter-Schaltflächen mit Anzahl; im
`VideoObject` landet das Thema als `genre`. Videos ohne Eintrag erscheinen unter
„Alle", tragen aber kein Etikett — es geht nichts verloren, wenn die Zuordnung
fehlt.

Das ist der **einzige Handgriff pro Video**: eine Zeile nachtragen. Bewusst
manuell, weil der Feed kein Thema liefert und Raten anhand von Stichwörtern
falsch einsortiert. Die Themennamen folgen den YouTube-Playlists
(History · Physics · Psychology · Anatomy).

Die Filterleiste ist im HTML `hidden` und wird erst per JavaScript eingeblendet.
Ohne JavaScript sieht man die vollständige, chronologische Liste statt toter
Schaltflächen.

## Impressum

Die Angaben stehen in `data/imprint.json`. Die Seite `/impressum/` wird **nur
gebaut und verlinkt**, wenn `operator` ausgefüllt ist und zusätzlich `email`
oder `address` — sonst warnt der Build und lässt die Seite weg. Ein Impressum
mit Lücken ist schlechter als keines.

Die Seite enthält neben den Betreiberangaben kurze Abschnitte zu Hosting
(GitHub Pages), zum YouTube-Embed und zu Google AdSense — alles drei
Datenverarbeitungen, die durch die Bauweise der Seite entstehen.

Alles in dieser Datei wird öffentlich und von Suchmaschinen indexiert.

## Werbung

`ADSENSE_CLIENT` in `scripts/build_site.py` hält die AdSense-Publisher-ID. Der
Tag wird in den `<head>` **jeder** Seite geschrieben. Leerer String schaltet ihn
überall ab.

Dazu zwei Punkte:

- **`ads.txt`** liegt im Repo-Root und wird wie die `CNAME` ins Artefakt
  kopiert, damit sie unter `/ads.txt` ausgeliefert wird. Der Build vergleicht
  die Publisher-ID darin mit `ADSENSE_CLIENT` und warnt bei Abweichung — ein
  stiller Zahlendreher dort kostet Einnahmen, ohne dass irgendwo ein Fehler
  auftaucht.
- **Einwilligung (EWR/UK/Schweiz).** Google verlangt für personalisierte Werbung
  an EWR-Nutzer eine zertifizierte Consent-Management-Plattform. Die Seite hat
  keine. Solange keine da ist, ist der Tag zwar eingebunden, aber die
  Einwilligungspflicht nicht erfüllt.

## Einwilligung, Werbung, Analytics

Drei Konstanten in `scripts/build_site.py` steuern alles:

| | |
|---|---|
| `ADSENSE_CLIENT` | AdSense-Publisher-ID |
| `ANALYTICS_ID` | GA4-Mess-ID (`G-…`), leer = aus |
| `CONSENT_DEFAULTS` | Google Consent Mode v2 |

**Die Reihenfolge im `<head>` ist der Kern und darf nicht verdreht werden:**

1. `CONSENT_DEFAULTS` — synchron, setzt `ad_storage`, `ad_user_data`,
   `ad_personalization` und `analytics_storage` auf `denied`
2. GA4-Tag
3. AdSense-Tag

Laufen die Google-Tags vor den Defaults, starten sie mit erteilter Einwilligung
und die Defaults kommen zu spät. Deshalb steht der Block synchron und zuerst.

**Die eigentliche Einwilligung holt AdSense ein**, nicht dieses Repo: die
Einwilligungsmeldung wird im AdSense-Konto unter *Datenschutz und Meldungen*
konfiguriert und aktualisiert zur Laufzeit den Consent-Zustand. Ohne diese
Konfiguration bleibt alles auf `denied` — die Seite funktioniert, aber Werbung
läuft unpersonalisiert und Analytics misst nichts.
