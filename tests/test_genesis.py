from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from raja_sts import (
    RUNTIME_REQUIREMENTS_KEY,
    RUNTIME_REQUIREMENTS_LIMITATIONS,
    RUNTIME_REQUIREMENTS_MAX_BYTES,
    RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_FINDINGS,
    RUNTIME_REQUIREMENTS_MAX_IMPORTS,
    RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS,
    RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS,
    RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS,
    RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS,
    RUNTIME_REQUIREMENTS_SCHEMA,
    RuntimeImportRequirement,
    RuntimeRequirementCheck,
    RuntimeRequirements,
    RuntimeRequirementsError,
    RuntimeRequirementsReport,
    parse_runtime_requirements,
    verify_runtime_requirements,
)


def _document(
    *,
    python: object = ">=3.11",
    imports: object = (),
    schema: object = RUNTIME_REQUIREMENTS_SCHEMA,
    **extra: object,
) -> bytes:
    value = {
        "$schema": schema,
        "python": python,
        "imports": list(imports) if isinstance(imports, tuple) else imports,
        **extra,
    }
    return json.dumps(value).encode()


def _requirement(module: object = "demo", requirement: object = "demo>=1") -> dict[str, object]:
    return {"module": module, "requirement": requirement}


def _pathological_json(kind: str) -> bytes:
    prefix = (
        b'{"$schema":'
        + json.dumps(RUNTIME_REQUIREMENTS_SCHEMA).encode()
        + b',"python":">=3.11","imports":'
    )
    if kind == "nested":
        return prefix + b"[" * 1_100 + b"]" * 1_100 + b"}"
    if kind == "integer":
        return prefix + b"1" * 5_000 + b"}"
    raise AssertionError(f"unknown pathological JSON kind: {kind}")


def test_empty_runtime_requirements_declaration_is_explicit_and_valid() -> None:
    parsed = parse_runtime_requirements(_document(imports=[]))

    assert parsed.schema == RUNTIME_REQUIREMENTS_SCHEMA
    assert parsed.python == ">=3.11"
    assert parsed.imports == ()
    assert parsed.report() == {
        "schema": RUNTIME_REQUIREMENTS_SCHEMA,
        "python": ">=3.11",
        "imports": [],
    }


def test_runtime_requirements_normalize_and_sort_imports_by_module() -> None:
    parsed = parse_runtime_requirements(
        _document(
            imports=[
                _requirement("zeta", 'Zeta_Package>=2; python_version >= "3.11"'),
                _requirement("PIL", "Pillow>=10"),
            ]
        )
    )

    assert [item.module for item in parsed.imports] == ["PIL", "zeta"]
    assert parsed.imports[0].distribution == "Pillow"
    assert parsed.imports[0].requirement == "Pillow>=10"
    assert parsed.imports[1].distribution == "Zeta_Package"
    assert parsed.imports[1].marker == 'python_version >= "3.11"'


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff", "UTF-8 JSON object"),
        (b"not json", "UTF-8 JSON object"),
        (b"[]", "must contain a JSON object"),
        (
            b'{"$schema":"x","$schema":"y","python":">=3.11","imports":[]}',
            "duplicate JSON key",
        ),
        (
            b'{"$schema":"https://quiltdata.com/schemas/raja-sts/'
            b'handler-runtime-requirements/v1","python":">=3.11",'
            b'"imports":[{"module":"demo","module":"other",'
            b'"requirement":"demo"}]}',
            "duplicate JSON key",
        ),
        (json.dumps({"python": ">=3.11", "imports": []}).encode(), "missing required field"),
        (_document(unexpected=True), "unknown field"),
        (_document(schema="https://example.test/wrong"), "must use schema"),
        (_document(python=""), "nonblank specifier"),
        (_document(python=3), "nonblank specifier"),
        (_document(python="not a specifier"), "invalid version specifier"),
        (_document(imports={}), "imports must be an array"),
        (_document(imports=["demo"]), "must be an object"),
        (
            _document(imports=[{"module": "demo"}]),
            "is missing required field",
        ),
        (
            _document(imports=[{**_requirement(), "other": True}]),
            "contains unknown field",
        ),
    ],
    ids=[
        "utf8",
        "json",
        "root",
        "duplicate-root",
        "duplicate-import-field",
        "missing-top-level",
        "unknown-top-level",
        "schema",
        "blank-python",
        "non-string-python",
        "python-specifier",
        "imports-array",
        "import-object",
        "missing-import-field",
        "unknown-import-field",
    ],
)
def test_runtime_requirements_reject_malformed_documents(content: bytes, message: str) -> None:
    with pytest.raises(RuntimeRequirementsError, match=message):
        parse_runtime_requirements(content)


@pytest.mark.parametrize("kind", ["nested", "integer"])
def test_pathological_json_is_normalized_to_a_typed_refusal(kind: str) -> None:
    with pytest.raises(RuntimeRequirementsError, match="UTF-8 JSON object") as raised:
        parse_runtime_requirements(_pathological_json(kind))

    assert isinstance(raised.value.__cause__, (RecursionError, ValueError))


@pytest.mark.parametrize(
    ("imports", "message"),
    [
        ([_requirement(1)], "top-level Python import identifier"),
        ([_requirement("")], "top-level Python import identifier"),
        ([_requirement("a.b")], "top-level Python import identifier"),
        ([_requirement("class")], "top-level Python import identifier"),
        ([_requirement(), _requirement()], "duplicate module"),
        ([_requirement(requirement="")], "nonblank PEP 508"),
        ([_requirement(requirement=1)], "nonblank PEP 508"),
        ([_requirement(requirement="not a requirement !")], "requirement is invalid"),
        (
            [_requirement(requirement="demo @ https://example.test/demo.whl")],
            "must not use a direct URL",
        ),
        ([_requirement(requirement="demo[gpu]>=1")], "must not use extras"),
    ],
    ids=[
        "module-type",
        "module-blank",
        "module-dotted",
        "module-keyword",
        "module-duplicate",
        "requirement-blank",
        "requirement-type",
        "requirement-invalid",
        "requirement-url",
        "requirement-extras",
    ],
)
def test_runtime_requirements_reject_ambiguous_import_declarations(
    imports: Sequence[Mapping[str, object]], message: str
) -> None:
    with pytest.raises(RuntimeRequirementsError, match=message):
        parse_runtime_requirements(_document(imports=imports))


def test_verifier_is_deterministic_and_keeps_distribution_separate_from_module() -> None:
    declaration = parse_runtime_requirements(
        _document(
            python=">=3.11,<3.12",
            imports=[
                _requirement("win_only", 'WindowsOnly==1; sys_platform == "win32"'),
                _requirement("PIL", 'Pillow>=10,<11; sys_platform == "linux"'),
            ],
        )
    )
    distribution_calls: list[str] = []
    module_calls: list[str] = []

    def distribution_version(name: str) -> str:
        distribution_calls.append(name)
        assert name == "Pillow"
        return "10.4.0"

    def module_finder(name: str) -> object | None:
        module_calls.append(name)
        assert name == "PIL"
        return object()

    report = verify_runtime_requirements(
        declaration,
        python_version="3.11.9",
        distribution_version=distribution_version,
        module_finder=module_finder,
        marker_environment={"sys_platform": "linux"},
    )

    assert distribution_calls == ["Pillow"]
    assert module_calls == ["PIL"]
    assert [
        (
            check.requirement.module,
            check.applicability,
            check.distribution_version_status,
            check.module_resolution_status,
        )
        for check in report.checks
    ] == [
        ("PIL", "applicable", "compatible", "resolvable"),
        ("win_only", "not-applicable", "not-checked", "not-checked"),
    ]
    assert report.report() == {
        "schema": RUNTIME_REQUIREMENTS_SCHEMA,
        "python": {
            "required": ">=3.11,<3.12",
            "actual": "3.11.9",
            "status": "compatible",
        },
        "summary": {"declared": 2, "applicable": 1, "not_applicable": 1},
        "imports": [
            {
                "requirement": 'Pillow<11,>=10; sys_platform == "linux"',
                "marker": 'sys_platform == "linux"',
                "applicability": "applicable",
                "distribution_version": {
                    "name": "Pillow",
                    "specifier": "<11,>=10",
                    "installed": "10.4.0",
                    "status": "compatible",
                },
                "module_resolution": {
                    "name": "PIL",
                    "status": "resolvable",
                },
            },
            {
                "requirement": 'WindowsOnly==1; sys_platform == "win32"',
                "marker": 'sys_platform == "win32"',
                "applicability": "not-applicable",
                "distribution_version": {
                    "name": "WindowsOnly",
                    "specifier": "==1",
                    "installed": None,
                    "status": "not-checked",
                },
                "module_resolution": {
                    "name": "win_only",
                    "status": "not-checked",
                },
            },
        ],
        "limitations": list(RUNTIME_REQUIREMENTS_LIMITATIONS),
    }


def test_context_only_marker_is_a_typed_refusal_not_a_silent_bypass() -> None:
    declaration = parse_runtime_requirements(
        _document(imports=[_requirement(requirement='demo>=1; extra == "gpu"')])
    )
    probes: list[str] = []

    with pytest.raises(RuntimeRequirementsError, match="marker cannot be evaluated"):
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=lambda name: probes.append(name) or "1",
            module_finder=lambda name: probes.append(name) or object(),
        )

    assert probes == []


@pytest.mark.parametrize(
    ("python", "installed", "resolvable", "message"),
    [
        ("<3", "1.5", True, "Python 3.11 does not satisfy <3"),
        (">=3.11", "0.9", True, "version 0.9 does not satisfy >=1"),
        (">=3.11", "not-a-version", True, "invalid installed version"),
        (">=3.11", "1.5", False, "module 'demo' is not resolvable"),
    ],
    ids=["python", "distribution-version", "invalid-installed-version", "module"],
)
def test_verifier_refuses_incompatible_python_distribution_or_module(
    python: str,
    installed: str,
    resolvable: bool,
    message: str,
) -> None:
    declaration = parse_runtime_requirements(
        _document(python=python, imports=[_requirement(requirement="demo>=1")])
    )

    with pytest.raises(RuntimeRequirementsError, match=message):
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=lambda _: installed,
            module_finder=lambda _: object() if resolvable else None,
        )


def test_verifier_refuses_missing_distribution_and_aggregates_probe_failures() -> None:
    declaration = parse_runtime_requirements(
        _document(
            imports=[
                _requirement("missing", "missing-distribution>=1"),
                _requirement("broken", "broken-distribution>=1"),
            ]
        )
    )

    def distribution_version(name: str) -> str:
        if name == "missing-distribution":
            raise importlib.metadata.PackageNotFoundError(name)
        raise RuntimeError("metadata database unavailable")

    def module_finder(name: str) -> object | None:
        if name == "broken":
            raise RuntimeError("finder unavailable")
        return None

    with pytest.raises(RuntimeRequirementsError) as raised:
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=distribution_version,
            module_finder=module_finder,
        )

    message = str(raised.value)
    assert "distribution 'missing-distribution' is not installed" in message
    assert "module 'missing' is not resolvable" in message
    assert "distribution 'broken-distribution' version lookup failed" in message
    assert "module 'broken' resolution failed" in message


def test_verifier_rejects_an_invalid_reported_python_version() -> None:
    declaration = parse_runtime_requirements(_document())

    with pytest.raises(RuntimeRequirementsError, match="Current Python version"):
        verify_runtime_requirements(declaration, python_version="not-a-version")


def test_module_resolution_does_not_execute_the_module_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "runtime_requirement_sentinel"
    sentinel = tmp_path / "executed"
    (tmp_path / f"{module_name}.py").write_text(
        f"open({str(sentinel)!r}, 'w').close()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    declaration = parse_runtime_requirements(
        _document(imports=[_requirement(module_name, "separate-distribution==1")])
    )

    report = verify_runtime_requirements(
        declaration,
        python_version="3.11",
        distribution_version=lambda _: "1",
    )

    assert report.checks[0].applicability == "applicable"
    assert report.checks[0].distribution_version_status == "compatible"
    assert report.checks[0].module_resolution_status == "resolvable"
    assert not sentinel.exists()


def test_runtime_requirements_source_size_has_an_exact_boundary() -> None:
    base = _document()
    at_limit = base + b" " * (RUNTIME_REQUIREMENTS_MAX_BYTES - len(base))

    assert parse_runtime_requirements(at_limit).imports == ()
    with pytest.raises(RuntimeRequirementsError, match="exceeds.*bytes"):
        parse_runtime_requirements(at_limit + b" ")


def test_runtime_requirements_import_count_has_an_exact_boundary() -> None:
    imports = [
        _requirement(f"module_{index:03d}", f"distribution-{index:03d}==1")
        for index in range(RUNTIME_REQUIREMENTS_MAX_IMPORTS)
    ]

    assert len(parse_runtime_requirements(_document(imports=imports)).imports) == (
        RUNTIME_REQUIREMENTS_MAX_IMPORTS
    )
    with pytest.raises(RuntimeRequirementsError, match="imports exceeds"):
        parse_runtime_requirements(
            _document(
                imports=[
                    *imports,
                    _requirement("one_too_many", "one-too-many==1"),
                ]
            )
        )


def test_runtime_requirement_string_limits_have_exact_boundaries() -> None:
    module_at_limit = "m" * RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS
    python_at_limit = "===" + "x" * (RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS - 3)
    requirement_prefix = 'demo; os_name == "'
    requirement_at_limit = (
        requirement_prefix
        + "x" * (RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS - len(requirement_prefix) - 1)
        + '"'
    )

    parsed = parse_runtime_requirements(
        _document(
            python=python_at_limit,
            imports=[_requirement(module_at_limit, requirement_at_limit)],
        )
    )
    assert len(parsed.python) == RUNTIME_REQUIREMENTS_MAX_PYTHON_CHARACTERS
    assert len(parsed.imports[0].module) == RUNTIME_REQUIREMENTS_MAX_MODULE_CHARACTERS
    assert len(parsed.imports[0].requirement) == RUNTIME_REQUIREMENTS_MAX_REQUIREMENT_CHARACTERS

    with pytest.raises(RuntimeRequirementsError, match="python exceeds"):
        parse_runtime_requirements(_document(python=python_at_limit + "x"))
    with pytest.raises(RuntimeRequirementsError, match="module exceeds"):
        parse_runtime_requirements(
            _document(imports=[_requirement(module_at_limit + "m", "demo==1")])
        )
    with pytest.raises(RuntimeRequirementsError, match="requirement exceeds"):
        parse_runtime_requirements(
            _document(imports=[_requirement("demo", requirement_at_limit + "x")])
        )


def test_diagnostics_are_bounded_after_all_bounded_probes() -> None:
    declaration = parse_runtime_requirements(
        _document(
            imports=[
                _requirement(f"module_{index:03d}", f"distribution-{index:03d}==1")
                for index in range(RUNTIME_REQUIREMENTS_MAX_IMPORTS)
            ]
        )
    )
    distribution_calls: list[str] = []
    module_calls: list[str] = []

    def missing_distribution(name: str) -> str:
        distribution_calls.append(name)
        raise importlib.metadata.PackageNotFoundError(name)

    def missing_module(name: str) -> object | None:
        module_calls.append(name)
        return None

    with pytest.raises(RuntimeRequirementsError) as raised:
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=missing_distribution,
            module_finder=missing_module,
        )

    assert len(distribution_calls) == RUNTIME_REQUIREMENTS_MAX_IMPORTS
    assert len(module_calls) == RUNTIME_REQUIREMENTS_MAX_IMPORTS
    assert (
        f"{RUNTIME_REQUIREMENTS_MAX_IMPORTS * 2 - RUNTIME_REQUIREMENTS_MAX_DIAGNOSTIC_FINDINGS} "
        "additional finding(s) omitted"
    ) in str(raised.value)
    assert len(str(raised.value)) < 4_000


def test_public_declarations_are_recanonicalized_before_reporting() -> None:
    declaration = RuntimeRequirements(
        python=">=3.11",
        imports=(RuntimeImportRequirement("PIL", "Pillow==10.4.0"),),
    )
    # Deliberately bypass frozen dataclass protections to simulate a malformed
    # object crossing an untyped API boundary. Verification must not trust the
    # redundant labels stored on that object.
    object.__setattr__(declaration, "schema", "forged-schema")
    object.__setattr__(declaration.imports[0], "distribution", "forged-distribution")
    object.__setattr__(declaration.imports[0], "marker", "forged-marker")

    report = verify_runtime_requirements(
        declaration,
        python_version="3.11",
        distribution_version=lambda name: "10.4.0" if name == "Pillow" else "0",
        module_finder=lambda name: object() if name == "PIL" else None,
    )

    evidence = report.report()
    assert evidence["schema"] == RUNTIME_REQUIREMENTS_SCHEMA
    assert evidence["imports"][0]["distribution_version"]["name"] == "Pillow"
    assert evidence["imports"][0]["marker"] is None


def test_invalid_public_declarations_and_private_results_fail_closed() -> None:
    with pytest.raises(RuntimeRequirementsError, match="requires a RuntimeRequirements"):
        verify_runtime_requirements(object())  # type: ignore[arg-type]
    with pytest.raises(RuntimeRequirementsError, match="top-level Python import"):
        RuntimeImportRequirement("dotted.module", "demo==1")
    with pytest.raises(TypeError, match="created by verify_runtime_requirements"):
        RuntimeRequirementCheck()
    with pytest.raises(TypeError, match="created by verify_runtime_requirements"):
        RuntimeRequirementsReport()


def test_public_declaration_container_invariants_fail_closed() -> None:
    item = RuntimeImportRequirement("demo", "demo==1")
    with pytest.raises(RuntimeRequirementsError, match="imports must be a sequence"):
        RuntimeRequirements(python=">=3.11", imports="demo")  # type: ignore[arg-type]
    with pytest.raises(RuntimeRequirementsError, match="imports exceeds"):
        RuntimeRequirements(
            python=">=3.11",
            imports=tuple(
                RuntimeImportRequirement(f"module_{index}", f"distribution-{index}==1")
                for index in range(RUNTIME_REQUIREMENTS_MAX_IMPORTS + 1)
            ),
        )
    with pytest.raises(RuntimeRequirementsError, match="validated declaration"):
        RuntimeRequirements(python=">=3.11", imports=(object(),))  # type: ignore[arg-type]
    with pytest.raises(RuntimeRequirementsError, match="duplicate module"):
        RuntimeRequirements(python=">=3.11", imports=(item, item))

    malformed = object.__new__(RuntimeRequirements)
    with pytest.raises(RuntimeRequirementsError, match="declaration is invalid"):
        verify_runtime_requirements(malformed)


def test_private_result_factories_enforce_cross_field_invariants() -> None:
    item = RuntimeImportRequirement("demo", "demo==1")
    with pytest.raises(RuntimeRequirementsError, match="marker-skipped"):
        RuntimeRequirementCheck._verified(
            item,
            applicability="not-applicable",
            installed_version="1",
            distribution_version_status="compatible",
            module_resolution_status="resolvable",
        )
    with pytest.raises(RuntimeRequirementsError, match="both successful probe outcomes"):
        RuntimeRequirementCheck._verified(
            item,
            applicability="applicable",
            installed_version=None,
            distribution_version_status="not-checked",
            module_resolution_status="not-checked",
        )
    skipped = RuntimeRequirementCheck._verified(
        item,
        applicability="not-applicable",
        installed_version=None,
        distribution_version_status="not-checked",
        module_resolution_status="not-checked",
    )
    with pytest.raises(RuntimeRequirementsError, match="do not match"):
        RuntimeRequirementsReport._verified(
            RuntimeRequirements(python=">=3.11", imports=()),
            actual_python="3.11",
            checks=(skipped,),
        )


def test_marker_environment_and_probe_values_are_bounded_typed_inputs() -> None:
    empty = parse_runtime_requirements(_document())
    with pytest.raises(RuntimeRequirementsError, match="marker_environment must be a mapping"):
        verify_runtime_requirements(empty, marker_environment=[])  # type: ignore[arg-type]
    with pytest.raises(RuntimeRequirementsError, match="exceeds 32 entries"):
        verify_runtime_requirements(
            empty,
            marker_environment={f"key-{index}": "value" for index in range(33)},
        )
    with pytest.raises(RuntimeRequirementsError, match="bounded strings"):
        verify_runtime_requirements(
            empty,
            marker_environment={
                "sys_platform": "x" * (RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS + 1)
            },
        )

    declaration = parse_runtime_requirements(_document(imports=[_requirement("demo", "demo==1")]))
    with pytest.raises(RuntimeRequirementsError, match="nonblank-string"):
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=lambda _: None,  # type: ignore[arg-type,return-value]
            module_finder=lambda _: object(),
        )
    with pytest.raises(RuntimeRequirementsError, match="installed version exceeds"):
        verify_runtime_requirements(
            declaration,
            python_version="3.11",
            distribution_version=lambda _: "1" * (RUNTIME_REQUIREMENTS_MAX_VERSION_CHARACTERS + 1),
            module_finder=lambda _: object(),
        )


def test_parser_refuses_nonbyte_content() -> None:
    with pytest.raises(RuntimeRequirementsError, match="content must be bytes"):
        parse_runtime_requirements("{}")  # type: ignore[arg-type]


def test_fixed_requirements_logical_key_is_public_and_stable() -> None:
    assert RUNTIME_REQUIREMENTS_KEY == "protocol/requirements.json"
