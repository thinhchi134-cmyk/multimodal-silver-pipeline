"""Single entry point for the Silver Transfer tools."""

import argparse
from importlib import import_module
from pathlib import Path
import sys


COMMANDS = {
    "fill": ("fill_silver", "Fill a Silver JSONL template from raw files"),
    "finalize": ("silver_finalize", "Audit Silver and export tables to Parquet"),
    "index": ("search_silver", "Build or query keyword and image indexes"),
    "audit-index": ("audit_index", "Inspect an existing search index"),
    "extend-images": ("extend_image_index", "Add large images to a copy of an index"),
    "text-semantic": ("text_semantic", "Build or query a semantic text index"),
    "recover": ("recover_unsupported", "Classify unsupported Silver records"),
    "recover-exotic": ("recover_exotic", "Extract previews, metadata and archive inventories"),
    "index-previews": ("index_previews", "Build or query a SigLIP2 preview index"),
    "auto-convert": ("auto_convert", "Experimentally convert SVG and DWG"),
    "bronze-demo": ("pipeline", "Run the original Bronze-to-Silver demo pipeline"),
    "legacy-fill": ("fill_template", "Run the original template filler"),
}


def invoke(command, arguments):
    module_name, _ = COMMANDS[command]
    module = import_module(f".{module_name}", package=__package__)
    module.main([str(argument) for argument in arguments])


def run(argv):
    from .ocr import add_arguments, validate_arguments

    parser = argparse.ArgumentParser(
        prog="silver-transfer run",
        description="Fill, optionally recover and index, then audit and export.",
    )
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    parser.add_argument("--recover", action="store_true", help="Classify and inspect unsupported formats")
    parser.add_argument("--auto-convert", action="store_true", help="Run experimental SVG/DWG adapters")
    parser.add_argument("--oda-path", type=Path)
    parser.add_argument("--index", action="store_true", help="Build a keyword index")
    image_mode = parser.add_mutually_exclusive_group()
    image_mode.add_argument("--images", action="store_true", help="Build an image index as well as keywords")
    image_mode.add_argument("--reuse-image-index", type=Path, help="Reuse validated image vectors from an earlier run")
    parser.add_argument("--semantic-text", action="store_true", help="Build keyword and semantic text indexes")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="cpu")
    add_arguments(parser)
    args = parser.parse_args(argv)
    validate_arguments(parser, args)

    raw = args.raw_root.resolve()
    template = args.template.resolve()
    out = args.out.resolve()
    if not raw.is_dir() or not template.is_file():
        parser.error("--raw-root must be a directory and --template must be a file")
    if out.is_relative_to(raw):
        parser.error("--out must be outside --raw-root")
    if out.exists():
        parser.error("--out already exists. Choose a new directory.")
    out.mkdir(parents=True)

    silver = out / "silver_filled.jsonl"
    base = out / "silver_base.jsonl" if args.recover or args.auto_convert else silver
    raw_args = ["--raw-root", raw]
    silver_args = ["--silver", silver, *raw_args]
    ocr_args = [
        "--ocr", args.ocr, "--ocr-device", args.ocr_device,
        "--ocr-min-confidence", args.ocr_min_confidence,
    ] if args.ocr else []
    invoke("fill", ["--template", template, "--output", base, "--verify-hash", *raw_args, *ocr_args])

    if args.recover or args.auto_convert:
        classified = out / "unsupported"
        invoke("recover", ["--silver", base, *raw_args, "--out", classified])
        sources = []
        enrichment_args = [
            "--enrichment", classified / "unsupported_enrichment.jsonl", *raw_args,
        ]
        if args.recover:
            invoke("recover-exotic", [*enrichment_args, "--out", out / "exotic"])
            sources.append(("recover-exotic", out / "exotic", "recovery_v2.jsonl"))
        if args.auto_convert:
            converter_args = [*enrichment_args, "--out", out / "converted"]
            if args.oda_path:
                converter_args.extend(["--oda-path", args.oda_path])
            invoke("auto-convert", converter_args)
            sources.append(("auto-convert", out / "converted", "results.jsonl"))
        from .merge_extraction import merge
        merge(base, silver, sources)

    index = out / "search"
    if args.index or args.images or args.reuse_image_index or args.semantic_text or args.ocr:
        image_args = ["--images"] if args.images else []
        invoke("index", ["build", *silver_args, "--out", index, "--device", args.device, *image_args])
    if args.reuse_image_index:
        from .reuse_images import reuse_image_vectors
        reuse_image_vectors(args.reuse_image_index, silver, index)
    if args.semantic_text:
        device = args.device
        if device == "auto":
            from .search_silver import device_choice
            device = device_choice(device)
        invoke("text-semantic", ["build", "--index", index, "--out", out / "semantic_text", "--device", device])
    audit_args = ["--image-index", index] if args.images or args.reuse_image_index else []
    invoke("finalize", [*silver_args, "--out", out / "handoff", *audit_args])
    print(f"Silver Transfer output: {out}")


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="silver-transfer",
        description="Convert Bronze project files into Silver artifacts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Commands:\n  run             Run several conversion stages together\n" + "\n".join(
            f"  {command:16} {description}" for command, (_, description) in COMMANDS.items()
        ),
    )
    parser.add_argument("command", choices=("run", *COMMANDS), metavar="COMMAND")
    if not arguments:
        parser.print_help()
        return
    command = parser.parse_args(arguments[:1]).command
    if command == "run":
        run(arguments[1:])
    else:
        invoke(command, arguments[1:])
