# Screenshots

Sottocartella con le immagini degli snapshot delle schermate (istruzione di consegna).
Sono catture reali dell'advisor in funzione sulla dashboard Snap4City, eseguito dalla
**Snap4City JupyterHub** con i due processi attivi (vedi `README.md` §3). La relazione in
`relazione/` richiama queste immagini.

## Immagini presenti

| File | Scenario | Cosa mostra |
|---|---|---|
| `01-tre-modi.png` | Tre modi in un turno (nessun modo indicato) | le tre rotte — i due itinerari monomodali (a piedi, in auto) e quello multimodale (trasporto pubblico) — disegnate insieme sulla mappa, con la risposta di confronto e il dock delle chip |
| `01-tre-modi-piedi.png` | Selezione chip "A piedi" | dopo il turno a tre modi, la chip ridisegna la sola rotta pedonale e apre la bolla di dettaglio |
| `01-tre-modi-auto.png` | Selezione chip "In auto" | la sola rotta in auto + dettaglio + pin dei parcheggi |
| `01-tre-modi-multimodale.png` | Selezione chip "In autobus" | il solo itinerario multimodale, con la geometria per-leg (tratti a piedi + tratto in bus) e i pin delle fermate |
| `02-modo-singolo-piedi.png` | Richiesta esplicita "a piedi" | turno a modo singolo: una sola rotta calcolata e disegnata |
| `02-modo-singolo-auto.png` | Richiesta esplicita "in auto" | idem, modo auto |
| `02-modo-singolo-multimodale.png` | Richiesta esplicita "in autobus" | idem, itinerario multimodale (non paga l'attesa degli altri modi) |
| `03-gps-vicino.png` | Destinazione per categoria vicino alla posizione | origine presa dal GPS del browser, destinazione risolta come servizio più vicino della categoria richiesta |
| `04-servizi-lungo-percorso.png` | Servizi lungo il percorso | rotta con i pin viola dei servizi della categoria richiesta, campionati lungo il tragitto |

## Note

- Le immagini sono catture della finestra della dashboard (chat box + widgetMap).
- I nomi dei file sono richiamati come figure nella relazione: se si rinominano, aggiornare
  di conseguenza i `\includegraphics` in `relazione/relazione.tex`.
