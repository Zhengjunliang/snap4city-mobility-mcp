# Screenshots

Catture reali dell'advisor in funzione sulla dashboard Snap4City, eseguito dalla
**Snap4City JupyterHub** con il **bridge attivo** (`uvicorn api:app --port 8010`; il server
MCP mobility e' ospitato sul server Snap4City, vedi `README.md` §3). La relazione in
`relazione/` richiama ogni immagine come figura di uno scenario.

Questo file e' anche la **lista di cattura**: la colonna *Come catturarla* indica cosa
digitare (e le condizioni, es. GPS del browser attivo) per riprodurre ogni scenario.
Gli indirizzi usano sempre il **numero civico completo** (cosi' la risoluzione civica e'
esercitata ovunque). Le localita' devono stare in **Toscana** (i dati km4city sono validi
solo li'). Ad ogni turno il bridge scrive `outputs.txt`: copiarlo nel file omonimo sotto
`examples/` (stesso numero di scenario).

## Scenari e catture

| File | Scenario | Come catturarla | Cosa mostra |
|---|---|---|---|
| `S01-tre-modi.png` | Tre modi (nessun modo indicato) | `da via Gramsci 50, Sesto Fiorentino a via della Stazione 1, Firenze` | le tre rotte (a piedi, auto, trasporto pubblico) disegnate insieme, il testo di confronto e il dock delle chip |
| `S02-tre-modi-piedi.png` | Chip "A piedi" | dopo S01, clic sulla chip *A piedi* | la sola rotta pedonale ridisegnata + bolla di dettaglio |
| `S03-tre-modi-auto.png` | Chip "In auto" | dopo S01, clic sulla chip *In auto* | la sola rotta in auto + dettaglio + pin dei parcheggi (blu) |
| `S04-tre-modi-autobus.png` | Chip "In autobus" | dopo S01, clic sulla chip *In autobus* | il solo itinerario multimodale con geometria per-tratta (piedi+bus) e pin delle fermate |
| `S05-senza-citta.png` | Destinazione senza citta' (candidato piu' vicino) | GPS attivo (es. a Firenze), poi `portami in via Giuseppe Verdi 10` | fra le vie omonime in piu' comuni toscani viene scelta quella piu' vicina alla posizione; indicando la citta' (S10) vince la citta' |
| `S06-modo-singolo-piedi.png` | Modo singolo esplicito, a piedi | `da via Cavour 12 a via de' Ginori 7 a piedi` | una sola rotta pedonale calcolata (non attende gli altri modi) |
| `S07-modo-singolo-auto.png` | Modo singolo esplicito, in auto | `da viale Morgagni 40 a piazza Beccaria 3 in auto` | una sola rotta in auto |
| `S08-modo-singolo-autobus.png` | Modo singolo esplicito, in autobus | `da via Bolognese 20 a via de' Serragli 5 in autobus` | il solo itinerario multimodale |
| `S09-origine-gps.png` | Origine presa dal GPS | GPS del browser attivo, poi `voglio andare a piazza del Duomo 1` (senza origine) | origine = posizione GPS (etichetta da geocodifica inversa), destinazione risolta |
| `S10-citta-nominata.png` | Citta' nominata nell'indirizzo | `da via Roma 10 a Firenze a viale Morgagni 40` | risoluzione con priorita' alla citta' indicata |
| `S11-follow-up.png` | Follow-up multi-turno | dopo S07 (in auto), digitare `e in autobus?` | il turno successivo riusa origine/destinazione dal contesto della conversazione |
| `S12-dest-categoria-supermercato.png` | Destinazione per categoria (supermercato) | GPS attivo, poi `portami al supermercato piu' vicino` | destinazione = servizio piu' vicino della categoria, con scala di raggio |
| `S13-dest-categoria-farmacia.png` | Destinazione per categoria (farmacia) | GPS attivo, poi `qual e' la farmacia piu' vicina?` | stessa funzione con categoria diversa |
| `S14-servizi-percorso-ristoranti.png` | Servizi lungo il percorso (ristoranti, auto) | `da via Luigi Castaldi 6 a via Goro Dati 8 in auto e i ristoranti lungo il percorso` | rotta in auto con i pin viola dei ristoranti campionati lungo il tragitto |
| `S15-servizi-percorso-autobus.png` | Servizi lungo il percorso (in autobus) | `da via Bolognese 20 a via de' Serragli 5 in autobus e le farmacie lungo il percorso` | in modo bus i servizi sono campionati solo attorno alle fermate di salita/discesa e ai tratti a piedi |
| `S16-nearby-farmacie.png` | Servizi vicini (farmacie, da GPS) | GPS attivo, poi `mostrami le farmacie qui intorno` | solo i pin delle farmacie attorno alla posizione, senza alcuna rotta |
| `S17-nearby-centro-nominato.png` | Servizi vicini (centro nominato) | `i ristoranti vicino a piazza Dalmazia` | ricerca centrata su un indirizzo/citta' indicato (non sul GPS) |
| `S18-nearby-parcheggi.png` | Servizi vicini (parcheggi) | GPS attivo, poi `dove posso parcheggiare qui vicino?` | i pin dei parcheggi attorno alla posizione |
| `S19-orario-partenza.png` | Orario di partenza | `da via Cavour 12 a via de' Serragli 5 in autobus alle 18` | l'orario indicato alimenta solo la pianificazione del trasporto pubblico |
| `S20-non-supportato.png` | Richiesta non supportata | `a che ora passa il treno?` | risposta cortese che indica la funzione non supportata |
| `S21-chiarimento.png` (opzionale) | Chiarimento di uno slot mancante | `mostrami le farmacie` (senza luogo) oppure `cosa c'e' qui intorno?` (senza categoria) | domanda di chiarimento ("dove?" / "che tipo?") senza cancellare la mappa |

## Note

- Le immagini sono catture della finestra della dashboard (chat box + widgetMap).
- S02-S04 sono sotto-stati interattivi dello stesso turno di S01: si catturano cliccando le
  chip dopo aver eseguito S01. S05 e' invece un turno a se' (risoluzione degli estremi).
- I nomi dei file sono richiamati come figure nella relazione: se si rinominano, aggiornare
  di conseguenza i `\includegraphics` in `relazione/relazione.tex`.
