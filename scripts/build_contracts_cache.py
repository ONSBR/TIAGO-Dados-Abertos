# -*- coding: utf-8 -*-
"""Gera contracts_cache.json: os YAMLs do corpus pre-parseados num JSON unico.

Motivo: sem o cache, cada instancia parseia os YAML no warmup e as primeiras
requests BLOQUEIAM nesse parse. Parsear um corpus grande custa segundos de CPU
(CLoader) e varias vezes isso a frio; json.loads fica abaixo de 1s.

Uso:
  python scripts/build_contracts_cache.py [--contracts-dir contracts]
O cache e ARTEFATO: quem publica corpus novo regera o cache junto (o runtime
usa o cache quando presente e cai para YAML quando ausente/invalido).

Datas: YAML produz datetime.date/datetime; JSON nao tem o tipo. Codificamos
como {"__date__": iso} / {"__datetime__": iso} e o catalog.py decodifica de
volta — round-trip fiel, sem pickle (sem risco de exec).

O cache inclui n_docs e content_hash para validacao de integridade no boot:
um cache truncado ou desatualizado e rejeitado, e o servidor cai para os YAML.
"""
import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _encode(o):
    if isinstance(o, datetime.datetime):
        return {"__datetime__": o.isoformat()}
    if isinstance(o, datetime.date):
        return {"__date__": o.isoformat()}
    raise TypeError(f"tipo nao serializavel no cache: {type(o)}")


def yaml_manifest(base: Path) -> str:
    """sha256 de (caminho relativo, tamanho, mtime_ns) de todo *.odcs.yaml sob base."""
    itens = sorted((str(p.relative_to(base)).replace(chr(92), '/'), p.stat().st_size, p.stat().st_mtime_ns)
                   for p in base.rglob('*.odcs.yaml'))
    return hashlib.sha256(json.dumps(itens).encode('utf-8')).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts-dir", default=str(Path(__file__).resolve().parents[1] / "contracts"))
    args = ap.parse_args()
    base = Path(args.contracts_dir)

    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    docs = []
    for orgao_dir in sorted(base.iterdir()):
        if not orgao_dir.is_dir() or orgao_dir.name.startswith("."):
            continue
        for f in sorted(orgao_dir.glob("*.odcs.yaml")):
            data = yaml.load(f.read_text(encoding="utf-8"), Loader=loader)
            if not data:
                continue
            docs.append({"orgao": orgao_dir.name, "stem": f.stem, "data": data})

    # Manifesto de integridade (F2): n_docs e content_hash para validacao no boot
    content_for_hash = json.dumps(docs, sort_keys=True, ensure_ascii=False, default=_encode)
    content_hash = hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()

    # Manifesto dos YAML em disco: um cache integro mas VELHO (YAML editado depois do build)
    # deixa de passar por valido — o carregador recomputa e compara.
    payload = {
        "version": 1,
        "n_docs": len(docs),
        "content_hash": content_hash,
        "yaml_manifest": yaml_manifest(base),
        "docs": docs,
    }

    out = base / "contracts_cache.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, default=_encode),
                   encoding="utf-8", newline="\n")
    print(f"cache: {len(docs)} docs -> {out} ({out.stat().st_size/1e6:.1f} MB)")
    print(f"  n_docs: {len(docs)}, content_hash: {content_hash[:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
