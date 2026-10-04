# Standard Fits

Drop one `.eft` file per fit in this folder, using the standard EVE fit
export format:

```
[Ship Name, Fit Name]

Item One
Item Two x3
...
```

## How it works

- **`Fit Name`** is the part after the comma on the first line. It is the name
  shown on the website for any contract whose items **100% match** the fit:
  the hull plus every listed item, with exact quantities.
- If a contract matches, the title the user gave the contract is ignored and
  the fit name is used instead — so "my typhoon deal", "brawl phoon" and
  "Typhoon: Brawl Phoon" all show up as `Brawl Phoon v4`.
- Item names are matched case-insensitively against in-game type names.

## Rules

- The hull comes from the first line only — don't list it again in the body.
- Quantities must match exactly (an `x1000` stack is not a match for two
  `x500` stacks).
- Any extra or missing item breaks the 100% match; that contract keeps its
  own title.
- Add a file, push it, and the scraper picks it up on its next `git pull`.
  Already-listed contracts are re-checked automatically, so existing
  contracts get renamed without needing to be re-posted.

## invTypes.csv (auto-managed — don't edit or commit)

`invTypes.csv` is the fuzzwork type dump used to translate in-game item
names to type IDs without hitting the ESI API. The scraper downloads it on
first run and refreshes it automatically when it gets older than one week.
It is ~20 MB and git-ignored.
