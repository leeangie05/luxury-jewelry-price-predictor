import json, sys
 
nb = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "notebooks/jewelry_price_predictor.ipynb"))
for cell in nb["cells"]:
    if cell["cell_type"] != "code":
        continue
    for out in cell.get("outputs", []):
        if "text" in out:
            print("".join(out["text"]), end="")
 