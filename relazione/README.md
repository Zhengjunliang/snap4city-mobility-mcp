# Relazione

Relazione dell'elaborato (Tipo A, *Sistemi Distribuiti*, UNIFI).

| File | Contenuto |
|---|---|
| `relazione.tex` | sorgente LaTeX (da consegnare insieme al PDF) |
| `relazione.pdf` | versione compilata — **da generare e depositare qui** |
| `Tptesi2.cls` | classe LaTeX del template UNIFI (non è una classe standard: va consegnata insieme al sorgente, altrimenti il documento non compila) |
| `img/stemma.pdf` | stemma dell'Ateneo usato nel frontespizio |

Il documento usa l'impaginazione del template (classe `book`: capitoli, testatine,
indice), ma **il frontespizio è scritto dentro `relazione.tex`** invece di usare il
`\maketitle` della classe: quest'ultimo riporta la dicitura «Tesi di Laurea Triennale»,
che non è corretta per un elaborato. La classe non è stata modificata.

## Figure

Le figure **non sono duplicate** in questa cartella: `relazione.tex` le prende dalle
cartelle del progetto tramite

```latex
\graphicspath{{figures/}{img/}{../docs/diagrams/}{../screenshots/}}
```

Le sette immagini incluse sono:

| Immagine | Provenienza |
|---|---|
| `stemma.pdf` | `relazione/img/` |
| `component.png`, `graph-flow.png`, `sequence.png`, `deployment.png` | `docs/diagrams/` |
| `01-tre-modi.png`, `04-servizi-lungo-percorso.png` | `screenshots/` |

## Compilazione su Overleaf (consigliata)

1. Comprimere **l'intera cartella del progetto** e caricarla su Overleaf
   (*New Project → Upload Project*). Caricando solo `relazione/` le figure dei
   diagrammi e delle schermate non verrebbero trovate, perché stanno nelle cartelle
   sorelle.
2. Impostare `relazione/relazione.tex` come documento principale
   (*Menu → Main document*).
3. Compilare con **pdfLaTeX**. La bibliografia è inclusa nel sorgente
   (`thebibliography`), quindi **non serve BibTeX**: due passate di pdfLaTeX bastano
   per risolvere indice e riferimenti alle figure.
4. Scaricare il PDF e salvarlo qui come `relazione.pdf`.

> `Tptesi2.cls` sta accanto a `relazione.tex`, quindi Overleaf la trova senza
> configurazione aggiuntiva.

### Variante: progetto Overleaf autonomo

Se si preferisce caricare solo questa cartella, copiare prima in `relazione/figures/`
le sei immagini che stanno fuori (i quattro diagrammi e le due schermate):
`\graphicspath` prova quel percorso per primo, quindi il sorgente compila senza
modifiche. Classe e stemma sono già in questa cartella.

## Compilazione locale

Con una distribuzione TeX installata (TeX Live, MiKTeX), dalla cartella `relazione/`:

```bash
pdflatex relazione.tex
pdflatex relazione.tex   # seconda passata: indice e riferimenti
```

La seconda passata è necessaria perché l'indice e i riferimenti alle figure sono
risolti solo dopo che il primo passaggio ha scritto il file ausiliario.
