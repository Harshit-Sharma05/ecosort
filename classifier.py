"""
EcoSort classifier: layered lookup that never returns "unknown".

Layers (first one that answers wins):
  1. exact       - name / alias match
  2. phrase      - a known item name appears inside the query ("old banana peel")
  3. fuzzy       - typo correction ("battrey", "plasitc bottle")
  4. keyword     - material words ("steel", "polythene", "leftover", "cable"...)
  5. ambiguous   - several materials fit ("bottle", "bulb"): ask user to pick
  6. ask         - guided questions (user tells us the material, so ALWAYS answerable)
  7. fallback    - safe general-waste advice (only if user answers "not sure")

Use:
    from classifier import Classifier
    clf = Classifier("waste_data.json")
    result = clf.classify("old banana peels")
    # or after guided questions:
    result = clf.from_answers({"material": "plastic", "special": "none"})
"""
import json, re, difflib, os, datetime

MAX_LOG_BYTES = 100_000   # stop logging unknown searches after about 100 KB

PRIORITY = ["hazardous", "electronic", "sanitary", "organic", "glass", "metal", "paper", "plastic", "general"]

# word -> material. Covers things not in the item list.
KEYWORDS = {
 "hazardous": """battery batteries cell chemical chemicals acid pesticide insecticide poison toxic mercury thinner solvent flammable
   syringe needle needles lancet sharps bandage medicine medicines tablet tablets capsule syrup antibiotic expired paint varnish
   petrol diesel kerosene fuel lubricant coolant antifreeze asbestos aerosol spray cfl fluorescent bleach disinfectant ink toner
   cartridge cylinder lighter thermometer biomedical infectious radioactive""",
 "electronic": """phone mobile charger adapter adaptor cable wire cord laptop computer pc keyboard mouse monitor screen television tv remote
   headphone headphones earphone earphones earbud earbuds speaker camera printer scanner router modem circuit electronic electric
   electrical appliance gadget device led tubelight drone console gamepad tablet ipad smartwatch watch calculator fan heater
   geyser fridge refrigerator washing microwave oven kettle toaster mixer grinder iron blender vacuum motor sensor chip
   motherboard processor ram harddisk ssd pendrive usb dongle inverter ups stabilizer solar""",
 "sanitary": """diaper diapers nappy nappies sanitary tampon tampons menstrual""",
 "organic": """food peel peels leftover leftovers scraps scrap fruit fruits vegetable vegetables veg meat bone bones fish egg eggs shell shells
   rice roti chapati bread dal curry cooked raw rotten spoiled stale compost leaf leaves grass flower flowers petals plant plants
   garden twig twigs branch husk peel seeds seed kernel stem stems weeds manure dung sawdust coconut tea coffee grounds milk curd
   butter cheese banana apple mango orange lemon potato onion tomato""",
 "glass": """glass jar jars tumbler beaker vial ampoule bulb? mirror window pane""",
 "metal": """metal steel iron aluminium aluminum copper brass bronze tin can cans foil utensil utensils nail nails screw screws bolt bolts
   scrap wire? rod pipe sheet hinge lock chain spoon fork knife pan pot kadhai tawa cooker tiffin hanger""",
 "paper": """paper cardboard carton newspaper magazine book books notebook notebooks copy copies envelope box boxes sheet sheets tissue?
   pamphlet flyer poster card cards brochure catalogue catalog receipt? tag tags sticky""",
 "plastic": """plastic polythene poly pet pvc hdpe ldpe pp bottle? bag bags wrapper wrappers packet packets pouch pouches container containers
   tub tubs tray trays cup cups straw straws bucket mug jug lid lids cap caps film bubble bowl basin crate bin toy toys""",
 "general": """cloth clothes clothing fabric textile cotton wool silk shirt pant pants jeans saree sock socks shoe shoes slipper slippers
   leather rubber foam sponge thermocol styrofoam diaper diapers sanitary pad pads napkin tampon wipes wipe hair dust sweepings
   ash ceramic porcelain crockery tile tiles brick bricks cement rubble debris candle candles mattress pillow carpet curtain
   laminated waxed greasy oily soiled dirty used cigarette butt butts gum mask masks glove gloves toothbrush razor""",
}
KEYWORDS = {m: set(w.replace("?", "") for w in s.split() if not w.endswith("?")) for m, s in KEYWORDS.items()}
# words ending with '?' above are weak hints; ignore them for safety (they conflict across materials)

QUESTIONS = [
 {"id": "material", "text": "What is the item mostly made of?",
  "options": ["Food / plant waste", "Plastic", "Paper / cardboard", "Glass", "Metal",
              "Electronic / has a plug or circuit board", "Battery / bulb / chemicals / medicines / sharp medical items",
              "Diaper / sanitary pad / similar hygiene item",
              "Cloth / rubber / ceramic / mixed", "Not sure"]},
 {"id": "special", "text": "Is it dirty with food/oil, or made of several layers (foil-lined, foam, laminated)?",
  "options": ["No, it is clean and single-material", "Yes, dirty / greasy / multi-layer"],
  "ask_if": ["Plastic", "Paper / cardboard"]},
]

ANSWER_TO_MATERIAL = {
 "Food / plant waste": "organic", "Plastic": "plastic", "Paper / cardboard": "paper", "Glass": "glass", "Metal": "metal",
 "Electronic / has a plug or circuit board": "electronic",
 "Battery / bulb / chemicals / medicines / sharp medical items": "hazardous",
 "Diaper / sanitary pad / similar hygiene item": "sanitary",
 "Cloth / rubber / ceramic / mixed": "general", "Not sure": "general",
}

STOPWORDS = {"ka", "ki", "ke", "ko", "the", "a", "an", "of", "my", "some", "this", "that", "is", "ek"}

def _norm(t):
    t = re.sub(r"[^a-z0-9\s]", " ", t.lower())
    words = [w for w in t.split() if w not in STOPWORDS]
    return " ".join(words)

def _singular(w):
    if len(w) > 4 and w.endswith("ies"): return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz": return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"): return w[:-1]
    return w

def _canon(t):
    return " ".join(_singular(w) for w in _norm(t).split())


class Classifier:
    def __init__(self, path="waste_data.json", unknown_log="unknown_queries.txt"):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.materials, self.items = d["materials"], d["items"]
        self.unknown_log = unknown_log
        self.index = {}        # canonical term -> item key
        self.related = {}      # canonical related term -> [item keys]
        for k, v in self.items.items():
            for t in [v["name"]] + v.get("aliases", []):
                self.index.setdefault(_canon(t), k)
            for t in v.get("related_terms", []):
                self.related.setdefault(_canon(t), []).append(k)
        self.terms = list(self.index)
        self.vocab = {w for t in self.terms for w in t.split() if len(w) > 2}
        self.kw = {}
        for m, words in KEYWORDS.items():
            for w in words:
                self.kw.setdefault(_singular(w), []).append(m)

    # ---------- result builders ----------
    def _item_result(self, key, how, query, score=1.0):
        it = self.items[key]; mat = self.materials[it["material"]]
        return {"status": "found", "method": how, "query": query, "matched": it["name"], "confidence": round(score, 2),
                "material": it["material"], "category": mat["category"], "bin": mat["bin"],
                "disposal": it.get("disposal") or mat["disposal"], "safety": it.get("safety") or mat["safety"]}

    def _material_result(self, material, how, query, note=None, score=0.6):
        mat = self.materials[material]
        return {"status": "found", "method": how, "query": query, "matched": note or mat["name"], "confidence": score,
                "material": material, "category": mat["category"], "bin": mat["bin"],
                "disposal": mat["disposal"], "safety": mat["safety"],
                "note": "We classified this by its material. Check the label or local rules if unsure.", "refine": score < 0.7 or how == "keyword"}

    # ---------- main entry ----------
    def classify(self, query):
        q = _canon(query)
        if not q:
            return {"status": "empty", "message": "Type the name of a waste item."}

        # 1. exact
        if q in self.index:
            return self._item_result(self.index[q], "exact", query)
        # 5. ambiguous generic term (bottle, bulb, pizza box)
        if q in self.related:
            keys = self.related[q]
            return {"status": "ambiguous", "query": query,
                    "message": f'"{query}" can go in different bins depending on what it is.',
                    "options": [{"item": k, "name": self.items[k]["name"], "material": self.items[k]["material"],
                                 "category": self.materials[self.items[k]["material"]]["category"]} for k in keys]}

        # 2. a known item name appears inside the query (longest, then safest, wins)
        padded = f" {q} "
        hits = [t for t in self.terms if len(t) >= 5 and f" {t} " in padded]
        if hits:
            hits.sort(key=lambda t: (-len(t), PRIORITY.index(self.items[self.index[t]]["material"])))
            best = hits[0]
            head = q.split()[-1]
            # "laptop bag": the matched term is not the head noun, so trust the last word if we know it
            if self.items[self.index[best]]["material"] not in ("hazardous", "electronic") and head not in best.split() and _singular(head) in self.kw and head not in ("waste", "scrap", "item", "items"):
                r = self._keyword_rule([head], query)
                if r: r["refine"] = True; return r
            return self._item_result(self.index[best], "phrase", query, 0.9)

        # 3. fuzzy whole-phrase
        for cand in difflib.get_close_matches(q, self.terms, n=5, cutoff=0.84):
            cw, qw = cand.split(), q.split()
            if len(cw) == len(qw) and all(a == b or (len(a) >= 5 and a[0] == b[0] and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8) for a, b in zip(cw, qw)):
                return self._item_result(self.index[cand], "fuzzy", query, 0.8)

        # 3b. fuzzy per word (fix typos word by word), then retry
        words = q.split(); fixed = []
        for w in words:
            if w in self.vocab or w in self.kw or len(w) < 5:
                fixed.append(w); continue
            c = [x for x in difflib.get_close_matches(w, list(self.vocab) + list(self.kw), n=3, cutoff=0.82) if x[0] == w[0]]
            fixed.append(c[0] if c else w)
        fq = " ".join(fixed)
        if fq != q:
            r = self.classify_corrected(fq, query)
            if r: return r

        # 4. keyword / material rules
        r = self._keyword_rule(words, query)
        if r: return r

        # 6. guided questions: always answerable
        self._log(query)
        return {"status": "ask", "query": query,
                "message": f'We could not match "{query}" directly. Answer a quick question and we will still tell you where it goes.',
                "questions": QUESTIONS}

    def classify_corrected(self, fq, original):
        if fq in self.index: return self._item_result(self.index[fq], "fuzzy", original, 0.8)
        padded = f" {fq} "
        hits = [t for t in self.terms if len(t) >= 5 and f" {t} " in padded]
        if hits:
            hits.sort(key=lambda t: (-len(t), PRIORITY.index(self.items[self.index[t]]["material"])))
            return self._item_result(self.index[hits[0]], "fuzzy", original, 0.75)
        return self._keyword_rule(fq.split(), original)

    def _keyword_rule(self, words, query):
        votes = {}
        for w in words:
            for m in self.kw.get(_singular(w), []):
                votes.setdefault(m, []).append(w)
        if not votes: return None
        head = _singular(words[-1]) if words else ''
        if head in self.kw and len(words) > 1:
            hm = self.kw[head]
            if not ({'hazardous','electronic'} & set(votes)):
                best = min(hm, key=PRIORITY.index)
                return self._material_result(best, 'keyword', query, note=f"Looks like {self.materials[best]['name'].lower()} (matched: {words[-1]})")
        # safety first: hazardous / electronic words win over everything else
        for m in PRIORITY:
            if m in votes and m in ("hazardous", "electronic"):
                return self._material_result(m, "keyword", query, note=f"Looks like {self.materials[m]['name'].lower()} (matched: {', '.join(votes[m])})")
        # soiled / dirty words push recyclables to general
        soiled = {"greasy", "oily", "dirty", "soiled", "used", "stained", "laminated", "waxed"}
        best = max(votes, key=lambda m: (len(votes[m]), -PRIORITY.index(m)))
        matched = votes[best]
        if best in ("paper", "plastic") and soiled & set(words):
            best = "general"
        return self._material_result(best, "keyword", query, note=f"Looks like {self.materials[best]['name'].lower()} (matched: {', '.join(matched)})")

    # ---------- guided answers ----------
    def from_answers(self, answers, query=""):
        mat = ANSWER_TO_MATERIAL.get(answers.get("material"), "general")
        if mat in ("plastic", "paper") and str(answers.get("special", "")).startswith("Yes"):
            mat = "general"
        r = self._material_result(mat, "guided", query, note=self.materials[mat]["name"], score=0.7)
        if answers.get("material") == "Not sure":
            r["note"] = "Not sure? Treat it as dry waste, wrap it if it is soiled or sharp, and check with your local municipal body."
            r["method"] = "fallback"; r["confidence"] = 0.4
        return r

    def _log(self, query):
        """Remember searches we could not match, so the item list can grow.

        The file stops growing at MAX_LOG_BYTES, so nobody can fill the disk.
        """
        try:
            if os.path.exists(self.unknown_log) and os.path.getsize(self.unknown_log) >= MAX_LOG_BYTES:
                return
            clean = " ".join(str(query).split())[:100]   # one line, no tabs, short
            if not clean:
                return
            with open(self.unknown_log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')}\t{clean}\n")
        except OSError:
            pass