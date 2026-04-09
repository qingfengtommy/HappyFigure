"""Code analysis tools — extract model architecture from Python/PyTorch source.

Parses Python files using AST analysis to extract class hierarchies, forward()
methods, layer definitions, loss functions, optimizers, and data flow.  Produces
structured JSON summaries suitable for the method drawing pipeline.

Functions:
    analyze_code_file   — full AST analysis of a single .py file
    analyze_code_dir    — batch analysis of all .py files in a directory
    code_to_method_description — LLM-assisted conversion from code summary to
                                  method description (same format as method_proposer_node)
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from typing import Any


# ── Known PyTorch layer/module types ──────────────────────────────────

_TORCH_LAYER_MODULES = {
    # Linear
    "Linear", "Bilinear", "LazyLinear",
    # Convolution
    "Conv1d", "Conv2d", "Conv3d", "ConvTranspose1d", "ConvTranspose2d",
    "ConvTranspose3d", "LazyConv1d", "LazyConv2d", "LazyConv3d",
    # Pooling
    "MaxPool1d", "MaxPool2d", "MaxPool3d", "AvgPool1d", "AvgPool2d",
    "AvgPool3d", "AdaptiveMaxPool1d", "AdaptiveMaxPool2d",
    "AdaptiveAvgPool1d", "AdaptiveAvgPool2d",
    # Normalization
    "BatchNorm1d", "BatchNorm2d", "BatchNorm3d", "LayerNorm",
    "GroupNorm", "InstanceNorm1d", "InstanceNorm2d", "InstanceNorm3d",
    # Activation (module form)
    "ReLU", "LeakyReLU", "PReLU", "ELU", "SELU", "GELU", "SiLU",
    "Mish", "Sigmoid", "Tanh", "Softmax", "LogSoftmax",
    # Dropout
    "Dropout", "Dropout2d", "Dropout3d", "AlphaDropout",
    # Recurrent
    "RNN", "LSTM", "GRU", "RNNCell", "LSTMCell", "GRUCell",
    # Transformer
    "Transformer", "TransformerEncoder", "TransformerDecoder",
    "TransformerEncoderLayer", "TransformerDecoderLayer",
    "MultiheadAttention",
    # Embedding
    "Embedding", "EmbeddingBag",
    # Container
    "Sequential", "ModuleList", "ModuleDict",
}

_TORCH_LOSS_FUNCTIONS = {
    "CrossEntropyLoss", "BCELoss", "BCEWithLogitsLoss", "MSELoss",
    "L1Loss", "SmoothL1Loss", "NLLLoss", "KLDivLoss", "CTCLoss",
    "TripletMarginLoss", "CosineEmbeddingLoss", "HingeEmbeddingLoss",
    "MarginRankingLoss", "MultiMarginLoss", "MultiLabelMarginLoss",
    "PoissonNLLLoss", "GaussianNLLLoss", "HuberLoss",
}

_TORCH_OPTIMIZERS = {
    "SGD", "Adam", "AdamW", "Adagrad", "Adadelta", "Adamax",
    "RMSprop", "LBFGS", "SparseAdam", "NAdam", "RAdam",
}

_TORCH_SCHEDULERS = {
    "StepLR", "MultiStepLR", "ExponentialLR", "CosineAnnealingLR",
    "ReduceLROnPlateau", "CyclicLR", "OneCycleLR",
    "CosineAnnealingWarmRestarts", "LinearLR", "ConstantLR",
    "SequentialLR", "ChainedScheduler", "PolynomialLR",
}


# ── AST Visitors ──────────────────────────────────────────────────────


class _ModuleVisitor(ast.NodeVisitor):
    """Extract nn.Module subclasses with their __init__ layers and forward() body."""

    def __init__(self) -> None:
        self.classes: list[dict] = []
        self._imports: dict[str, str] = {}  # alias -> full name

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._imports[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            full = f"{module}.{alias.name}" if module else alias.name
            self._imports[alias.asname or alias.name] = full
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = [_unparse_node(b) for b in node.bases]
        is_module = any(
            "Module" in b or "nn.Module" in b
            for b in bases
        )
        if not is_module:
            self.generic_visit(node)
            return

        cls_info: dict[str, Any] = {
            "name": node.name,
            "bases": bases,
            "lineno": node.lineno,
            "layers": [],
            "forward_args": [],
            "forward_body": "",
            "forward_calls": [],
            "forward_returns": [],
            "docstring": ast.get_docstring(node) or "",
        }

        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                cls_info["layers"] = _extract_init_layers(item)
            elif isinstance(item, ast.FunctionDef) and item.name == "forward":
                cls_info["forward_args"] = _extract_forward_args(item)
                cls_info["forward_body"] = _safe_unparse(item)
                cls_info["forward_calls"] = _extract_forward_calls(item)
                cls_info["forward_returns"] = _extract_returns(item)

        self.classes.append(cls_info)
        self.generic_visit(node)


def _extract_init_layers(init_node: ast.FunctionDef) -> list[dict]:
    """Extract self.<name> = nn.<Layer>(...) assignments from __init__."""
    layers = []
    for node in ast.walk(init_node):
        if not isinstance(node, ast.Assign):
            continue
        # Look for self.<attr> = <call>
        for target in node.targets:
            if not (isinstance(target, ast.Attribute) and
                    isinstance(target.value, ast.Name) and
                    target.value.id == "self"):
                continue
            attr_name = target.attr
            call_str = _unparse_node(node.value)
            layer_type = _classify_layer(node.value, call_str)
            args_info = _extract_call_args(node.value) if isinstance(node.value, ast.Call) else {}
            layers.append({
                "name": attr_name,
                "definition": call_str,
                "layer_type": layer_type,
                "args": args_info,
                "lineno": node.lineno,
            })
    return layers


def _classify_layer(node: ast.expr, call_str: str) -> str:
    """Classify a layer assignment into a category."""
    if isinstance(node, ast.Call):
        func_name = _unparse_node(node.func)
        # Strip module prefix: nn.Linear -> Linear
        short = func_name.rsplit(".", 1)[-1] if "." in func_name else func_name
        if short in _TORCH_LAYER_MODULES:
            return short
        if short in _TORCH_LOSS_FUNCTIONS:
            return f"loss:{short}"
    # Fallback: check string
    for name in _TORCH_LAYER_MODULES:
        if name in call_str:
            return name
    for name in _TORCH_LOSS_FUNCTIONS:
        if name in call_str:
            return f"loss:{name}"
    return "custom"


def _extract_call_args(call_node: ast.Call) -> dict:
    """Extract positional and keyword arguments from a Call node."""
    info: dict[str, Any] = {}
    for i, arg in enumerate(call_node.args):
        info[f"arg{i}"] = _unparse_node(arg)
    for kw in call_node.keywords:
        if kw.arg:
            info[kw.arg] = _unparse_node(kw.value)
    return info


def _extract_forward_args(func_node: ast.FunctionDef) -> list[str]:
    """Extract argument names from forward(self, ...)."""
    args = []
    for arg in func_node.args.args:
        if arg.arg == "self":
            continue
        annotation = ""
        if arg.annotation:
            annotation = _unparse_node(arg.annotation)
        args.append(f"{arg.arg}: {annotation}" if annotation else arg.arg)
    return args


def _extract_forward_calls(func_node: ast.FunctionDef) -> list[str]:
    """Extract self.<layer>(...) calls in forward() to trace data flow."""
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func_str = _unparse_node(node.func)
            if func_str.startswith("self."):
                calls.append(func_str[5:])  # strip "self."
    return calls


def _extract_returns(func_node: ast.FunctionDef) -> list[str]:
    """Extract return expressions from a function."""
    returns = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value:
            returns.append(_unparse_node(node.value))
    return returns


class _TrainingVisitor(ast.NodeVisitor):
    """Extract optimizer, scheduler, loss, and training loop patterns."""

    def __init__(self) -> None:
        self.optimizers: list[dict] = []
        self.schedulers: list[dict] = []
        self.loss_functions: list[dict] = []
        self.data_loaders: list[dict] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        target_str = _unparse_node(node.targets[0]) if node.targets else ""

        if isinstance(node.value, ast.Call):
            func_name = _unparse_node(node.value.func)
            short = func_name.rsplit(".", 1)[-1]

            if short in _TORCH_OPTIMIZERS:
                self.optimizers.append({
                    "name": target_str,
                    "type": short,
                    "args": _extract_call_args(node.value),
                    "lineno": node.lineno,
                })
            elif short in _TORCH_SCHEDULERS:
                self.schedulers.append({
                    "name": target_str,
                    "type": short,
                    "args": _extract_call_args(node.value),
                    "lineno": node.lineno,
                })
            elif short in _TORCH_LOSS_FUNCTIONS:
                self.loss_functions.append({
                    "name": target_str,
                    "type": short,
                    "args": _extract_call_args(node.value),
                    "lineno": node.lineno,
                })
            elif "DataLoader" in func_name:
                self.data_loaders.append({
                    "name": target_str,
                    "args": _extract_call_args(node.value),
                    "lineno": node.lineno,
                })

        self.generic_visit(node)


# ── AST Helpers ───────────────────────────────────────────────────────


def _unparse_node(node: ast.AST) -> str:
    """Unparse an AST node to source code string."""
    try:
        return ast.unparse(node)
    except Exception:
        return "<unknown>"


def _safe_unparse(node: ast.AST) -> str:
    """Unparse with truncation for large bodies."""
    try:
        text = ast.unparse(node)
        if len(text) > 2000:
            return text[:2000] + "\n# ... (truncated)"
        return text
    except Exception:
        return "<unparse failed>"


# ── Public API ────────────────────────────────────────────────────────


def analyze_code_file(file_path: str, base_dir: Path | None = None) -> dict:
    """Analyze a Python file and extract model architecture info.

    Returns a JSON-serializable dict with:
    - file: source file path
    - imports: list of import statements
    - model_classes: nn.Module subclasses with layers and forward()
    - training: optimizer, scheduler, loss, data loader info
    - summary: high-level overview suitable for method description generation
    """
    # Sandbox check if base_dir provided
    path = Path(file_path)
    if base_dir is not None:
        from tools.sandbox import safe_resolve
        resolved = safe_resolve(file_path, base_dir)
        if resolved is None:
            return {"error": f"Path outside sandbox: {file_path}"}
        path = resolved

    if not path.exists():
        return {"error": f"File not found: {file_path}"}
    if path.suffix != ".py":
        return {"error": f"Not a Python file: {file_path}"}

    try:
        source = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": f"Read error: {exc}"}

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {"error": f"Syntax error: {exc}"}

    # Extract module classes
    module_visitor = _ModuleVisitor()
    module_visitor.visit(tree)

    # Extract training components
    training_visitor = _TrainingVisitor()
    training_visitor.visit(tree)

    # Build summary
    model_classes = module_visitor.classes
    summary = _build_summary(model_classes, training_visitor)

    return {
        "file": str(path.name),
        "model_classes": model_classes,
        "training": {
            "optimizers": training_visitor.optimizers,
            "schedulers": training_visitor.schedulers,
            "loss_functions": training_visitor.loss_functions,
            "data_loaders": training_visitor.data_loaders,
        },
        "summary": summary,
    }


def analyze_code_dir(
    directory: str,
    base_dir: Path | None = None,
    max_files: int = 20,
) -> dict:
    """Analyze all .py files in a directory and return combined architecture info.

    Returns a dict with per-file results and an aggregated summary.
    """
    dir_path = Path(directory)
    if base_dir is not None:
        from tools.sandbox import safe_resolve
        resolved = safe_resolve(directory, base_dir)
        if resolved is None:
            return {"error": f"Path outside sandbox: {directory}"}
        dir_path = resolved

    if not dir_path.is_dir():
        return {"error": f"Not a directory: {directory}"}

    py_files = sorted(dir_path.rglob("*.py"))[:max_files]
    if not py_files:
        return {"error": f"No .py files found in {directory}"}

    results = []
    all_classes = []
    all_training: dict[str, list] = {
        "optimizers": [], "schedulers": [],
        "loss_functions": [], "data_loaders": [],
    }

    for py_file in py_files:
        result = analyze_code_file(str(py_file))
        if "error" not in result:
            results.append(result)
            all_classes.extend(result.get("model_classes", []))
            training = result.get("training", {})
            for key in all_training:
                all_training[key].extend(training.get(key, []))

    # Build aggregated summary
    agg_summary = _build_summary(all_classes, type("_T", (), all_training)())

    return {
        "directory": str(dir_path.name),
        "files_analyzed": len(results),
        "file_results": results,
        "aggregated": {
            "total_model_classes": len(all_classes),
            "model_names": [c["name"] for c in all_classes],
            "training": all_training,
            "summary": agg_summary,
        },
    }


def _build_summary(
    model_classes: list[dict],
    training_visitor: Any,
) -> dict:
    """Build a high-level summary from extracted components."""
    components = []
    data_flow_steps = []

    for cls in model_classes:
        # Build component entry
        layer_types = [ly["layer_type"] for ly in cls["layers"] if ly["layer_type"] != "custom"]
        component = {
            "name": cls["name"],
            "type": "nn.Module",
            "bases": cls["bases"],
            "layer_count": len(cls["layers"]),
            "key_layers": layer_types,
            "inputs": cls["forward_args"],
            "outputs": cls["forward_returns"],
            "docstring": cls["docstring"][:200] if cls["docstring"] else "",
        }
        components.append(component)

        # Build data flow from forward() calls
        if cls["forward_calls"]:
            steps = []
            for i, call in enumerate(cls["forward_calls"]):
                steps.append(f"{cls['name']}.{call}")
            data_flow_steps.append({
                "class": cls["name"],
                "forward_call_sequence": steps,
            })

    # Training info
    optimizers = [
        {"name": o.get("name", ""), "type": o.get("type", "")}
        for o in getattr(training_visitor, "optimizers", [])
    ]
    loss_fns = [
        {"name": fn.get("name", ""), "type": fn.get("type", "")}
        for fn in getattr(training_visitor, "loss_functions", [])
    ]

    return {
        "components": components,
        "data_flow": data_flow_steps,
        "optimizers": optimizers,
        "loss_functions": loss_fns,
    }


def code_to_method_description(
    code_analysis: dict,
    *,
    title: str | None = None,
) -> str:
    """Convert a code analysis result to a method description string.

    Produces a structured markdown description in the same format expected
    by the method drawing pipeline (Title, Overview, Components, Data Flow,
    Visual Layout, Drawing Instruction sections).

    This is a pure-format conversion (no LLM call). For LLM-enhanced
    descriptions, pass the output of this function to the method_proposer_node
    or use code_to_method_description_llm().
    """
    if "error" in code_analysis:
        return f"Error: {code_analysis['error']}"

    summary = code_analysis.get("summary", {})
    components = summary.get("components", [])
    data_flow = summary.get("data_flow", [])
    optimizers = summary.get("optimizers", [])
    loss_fns = summary.get("loss_functions", [])

    # Determine if this is from a single file or directory analysis
    model_classes = code_analysis.get("model_classes", [])
    if not model_classes:
        # Directory analysis
        agg = code_analysis.get("aggregated", {})
        model_classes = []
        for fr in code_analysis.get("file_results", []):
            model_classes.extend(fr.get("model_classes", []))
        components = agg.get("summary", {}).get("components", components)
        data_flow = agg.get("summary", {}).get("data_flow", data_flow)

    if not components:
        return "No model classes found in the analyzed code."

    # Title
    main_class = components[0]["name"] if components else "Model"
    desc_title = title or f"{main_class} Architecture"

    # Overview
    overview_parts = []
    for comp in components:
        layers = comp.get("key_layers", [])
        if layers:
            layer_summary = ", ".join(sorted(set(layers)))
            overview_parts.append(
                f"{comp['name']} ({comp['type']}) with {comp['layer_count']} layers "
                f"including {layer_summary}"
            )
        else:
            overview_parts.append(
                f"{comp['name']} ({comp['type']}) with {comp['layer_count']} layers"
            )

    # Components section
    comp_lines = []
    for i, comp in enumerate(components, 1):
        inputs = ", ".join(comp.get("inputs", [])) or "tensor"
        outputs = ", ".join(comp.get("outputs", [])) or "tensor"
        doc = comp.get("docstring", "")
        function_desc = doc if doc else f"Neural network module with {comp['layer_count']} layers"

        comp_lines.append(
            f"{i}. **{comp['name']}**\n"
            f"   - Type: {comp['type']} (inherits from {', '.join(comp.get('bases', []))})\n"
            f"   - Function: {function_desc}\n"
            f"   - Inputs: {inputs}\n"
            f"   - Outputs: {outputs}\n"
            f"   - Key layers: {', '.join(comp.get('key_layers', [])) or 'custom layers'}"
        )

    # Data Flow section
    flow_lines = []
    step_num = 1
    for df in data_flow:
        for call in df["forward_call_sequence"]:
            flow_lines.append(f"{step_num}. {call}")
            step_num += 1

    # Visual Layout
    n_components = len(components)
    direction = "top-to-bottom" if n_components <= 3 else "left-to-right"
    has_parallel = any(
        len(df.get("forward_call_sequence", [])) > 5 for df in data_flow
    )

    # Detect groupings from class hierarchy
    groupings = []
    for comp in components:
        layers = comp.get("key_layers", [])
        if len(layers) >= 3:
            groupings.append(f"{comp['name']} (contains {', '.join(layers[:5])})")

    # Training components
    training_lines = []
    if loss_fns:
        for lf in loss_fns:
            training_lines.append(f"- Loss: {lf['type']} ({lf['name']})")
    if optimizers:
        for opt in optimizers:
            training_lines.append(f"- Optimizer: {opt['type']} ({opt['name']})")

    # Assemble
    md = f"""## Title
{desc_title}

## Overview
{'; '.join(overview_parts)}.{(' Training uses ' + ', '.join(lf['type'] for lf in loss_fns) + ' loss.') if loss_fns else ''}

## Components
{chr(10).join(comp_lines)}

## Data Flow
{chr(10).join(flow_lines) if flow_lines else 'Data flow extracted from forward() methods of the model classes above.'}

## Visual Layout
- **Direction**: {direction}
- **Groupings**: {'; '.join(groupings) if groupings else 'Each model class as a separate group'}
- **Parallel paths**: {'Yes — multiple branches detected' if has_parallel else 'Sequential flow'}
{chr(10).join(training_lines) if training_lines else ''}

## Drawing Instruction
Draw a publication-quality architecture diagram for {desc_title}.
"""

    # Add per-component drawing details
    for comp in components:
        layers = comp.get("key_layers", [])
        md += f"\n### {comp['name']}\n"
        if layers:
            md += f"Show as a group box containing sub-blocks for: {', '.join(layers)}.\n"
        inputs = comp.get("inputs", [])
        outputs = comp.get("outputs", [])
        if inputs:
            md += f"Inputs enter from the {'top' if direction == 'top-to-bottom' else 'left'}: {', '.join(inputs)}.\n"
        if outputs:
            md += f"Outputs exit from the {'bottom' if direction == 'top-to-bottom' else 'right'}: {', '.join(outputs)}.\n"

    # Data flow arrows
    if data_flow:
        md += "\n### Data Flow Arrows\n"
        for df in data_flow:
            calls = df["forward_call_sequence"]
            if len(calls) >= 2:
                for i in range(len(calls) - 1):
                    md += f"- {calls[i]} -> {calls[i+1]}\n"

    return md.strip()


def code_to_method_description_llm(
    code_analysis: dict,
    *,
    title: str | None = None,
    model_mode: str = "chat",
) -> str:
    """LLM-enhanced conversion from code analysis to method description.

    Uses the llm module's run_prompt to produce a richer, more narrative
    method description from the structured code analysis.
    """
    # First, build the raw structured description
    raw_description = code_to_method_description(code_analysis, title=title)
    if raw_description.startswith("Error:") or raw_description.startswith("No model"):
        return raw_description

    # Prepare the code summary as context
    code_json = json.dumps(code_analysis, indent=2, default=str)
    if len(code_json) > 8000:
        code_json = code_json[:8000] + "\n... (truncated)"

    import llm

    system_prompt = textwrap.dedent("""\
        You are a Scientific Method Architecture Analyst. Given a structured
        code analysis of a PyTorch model, produce a publication-quality
        method description suitable for generating an architecture diagram.

        Your output MUST follow this exact section structure:
        ## Title
        ## Overview
        ## Components
        ## Data Flow
        ## Visual Layout
        ## Drawing Instruction

        Be specific about shapes, colors, arrows, and layout in the Drawing
        Instruction. Use professional scientific diagram conventions.
    """)

    user_prompt = (
        f"Here is a structured analysis of the codebase:\n\n"
        f"```json\n{code_json}\n```\n\n"
        f"And here is a draft method description:\n\n{raw_description}\n\n"
        f"Please improve and expand this into a complete, publication-quality "
        f"method description. Keep all the technical details from the code "
        f"analysis but make the description more narrative and suitable for "
        f"generating a clear architecture diagram."
    )

    response = llm.run_prompt(
        model_mode,
        user_prompt,
        system_prompt=system_prompt,
    ).strip()

    return response


# ── Dispatcher ────────────────────────────────────────────────────────

_TOOL_DISPATCH = {
    "analyze_code_file": lambda args, rd: analyze_code_file(
        file_path=args["file_path"],
        base_dir=rd,
    ),
    "analyze_code_dir": lambda args, rd: analyze_code_dir(
        directory=args["directory"],
        base_dir=rd,
        max_files=args.get("max_files", 20),
    ),
    "code_to_description": lambda args, rd: {
        "method_description": code_to_method_description(
            code_analysis=args["code_analysis"],
            title=args.get("title"),
        ),
    },
}


def execute_code_tool(tool_name: str, args: dict, results_dir: Path) -> dict:
    """Dispatch a code-analysis tool call by name."""
    handler = _TOOL_DISPATCH.get(tool_name)
    if handler is None:
        return {"error": f"Unknown code tool: {tool_name}"}
    try:
        return handler(args, results_dir)
    except Exception as exc:
        return {"error": f"Tool execution failed: {exc}"}
