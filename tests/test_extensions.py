import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from micode import scan, symbols  # noqa: E402

HEADER = """#ifndef FIELD_H
#define FIELD_H
class Field
{
public:
    Field(int size);
    double norm() const;
private:
    int size_;
};
#endif
"""
SOURCE = """#include "Field.H"

Field::Field(int size)
:
    size_(size)
{}

double Field::norm() const
{
    return size_ * 1.0;
}
"""


class Parse(unittest.TestCase):
    def test_forms_and_aliases(self):
        m = scan._parse_mapping({".H": "C++", "*.C": "cpp", "SConstruct": "py"})
        self.assertEqual(m, {".H": "cpp", ".C": "cpp", "SConstruct": "python"})
        self.assertEqual(scan._parse_mapping([".inl=cxx", "bad"]), {".inl": "cpp"})

    def test_case_sensitive_match(self):
        custom = {".C": "cpp", ".H": "cpp"}
        self.assertEqual(scan._lang("src/Field.C", custom), "cpp")
        self.assertEqual(scan._lang("src/Field.H", custom), "cpp")
        self.assertEqual(scan._lang("src/util.c", custom), "c")      # built-in .c stays C
        self.assertEqual(scan._lang("src/Field.C"), "")              # unknown without a mapping

    def test_precedence(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as repo:
            user = os.path.join(home, "config.json")
            json.dump({"extensions": {".H": "c", ".T": "cpp"}}, open(user, "w"))
            json.dump({"extensions": {".H": "cpp"}}, open(os.path.join(repo, ".mic.json"), "w"))
            with mock.patch.object(scan, "USER_CONFIG", user), mock.patch.dict(os.environ, {"MIC_EXTENSIONS": ".T=python"}):
                self.assertEqual(scan.load_languages(repo), {".H": "cpp", ".T": "python"})   # repo > user, env > repo
                self.assertEqual(scan.load_languages(repo, [".T=rust"])[".T"], "rust")      # --ext wins


class ScanRepo(unittest.TestCase):
    def test_dot_H_and_dot_C_become_cpp_with_symbols(self):
        with tempfile.TemporaryDirectory() as repo:
            open(os.path.join(repo, "Field.H"), "w").write(HEADER)
            open(os.path.join(repo, "Field.C"), "w").write(SOURCE)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            with mock.patch.object(scan, "USER_CONFIG", "/nonexistent"):
                auto = {f.path: f.lang for f in scan.scan(repo)}                           # zero config: GCC/OpenFOAM convention
                self.assertEqual(auto, {"Field.C": "cpp", "Field.H": "cpp"})
                with open(os.path.join(repo, ".mic.json"), "w") as fh:
                    json.dump({"auto_extensions": False}, fh)
                self.assertEqual(scan.scan(repo), [])                                      # opt-out honoured
                files = {f.path: f for f in scan.scan(repo, scan.load_languages(repo, [".H=cpp", ".C=cpp"]))}
            self.assertEqual({p: f.lang for p, f in files.items()}, {"Field.C": "cpp", "Field.H": "cpp"})
            syms, imps = symbols.extract(SOURCE, "cpp")
            names = {s["name"] for s in syms}
            self.assertIn("Field::norm", names)
            self.assertEqual(imps, ["Field.H"])
            graph = symbols.resolve_imports({"Field.C": imps, "Field.H": []}, {"Field.C": "cpp", "Field.H": "cpp"})
            self.assertEqual(graph["Field.C"], ["Field.H"])


if __name__ == "__main__":
    unittest.main()


class CppNames(unittest.TestCase):
    def test_resolver_and_where_handle_cpp_qualifiers(self):
        from micode import store
        syms, _ = symbols.extract(SOURCE, "cpp")
        r = store.Resolver({"Field.C": syms}, {"Field.C": SOURCE.count("\n")})
        norm = next(s for s in syms if s["name"] == "Field::norm")
        span = f"Field.C:L{norm['start']}-{norm['end']}"
        self.assertEqual(r.ref("Field.C::Field::norm"), span)
        self.assertEqual(r.ref("Field.C::norm"), span)
        self.assertEqual(r.ref("Field.C::L3"), "Field.C:L3-3")
        self.assertIn(f"(L{norm['start']}-{norm['end']})", r.annotate("see `Field.C::Field::norm` for details"))


class CppSymbols(unittest.TestCase):
    def test_definitions_declarations_and_no_false_calls(self):
        src_syms = {s["name"]: s for s in symbols.extract(SOURCE + "\nFoam::scalar Foam::Field::rms() const\n{\n    return std::sqrt(1.0);\n}\n", "cpp")[0]}
        self.assertIn("Field::Field", src_syms)                 # constructor at column 0, no return type
        self.assertIn("Field::norm", src_syms)
        self.assertIn("Foam::Field::rms", src_syms)             # OpenFOAM-style qualified return type
        self.assertNotIn("std::sqrt", src_syms)                 # calls inside bodies are not definitions
        hdr = {s["name"]: s for s in symbols.extract(HEADER, "cpp")[0]}
        self.assertEqual(hdr["Field.norm"]["kind"], "declaration")
        self.assertIn("Field.Field", hdr)                       # constructor declaration
        self.assertEqual(hdr["Field.norm"]["start"], hdr["Field.norm"]["end"])


class V11(unittest.TestCase):
    def test_extensions_without_language(self):
        self.assertEqual(scan._parse_mapping([".H,.C,.hpp,.cpp"]), {".H": "cpp", ".C": "cpp", ".hpp": "cpp", ".cpp": "cpp"})
        self.assertEqual(scan._parse_mapping([".H", ".C"]), {".H": "cpp", ".C": "cpp"})
        self.assertEqual(scan._parse_mapping([".weird"]), {})            # unknown and no language: skipped with a note

    def test_micignore_rules(self):
        r = scan.Rules(["# comment", "build/", "/tutorials", "*.dep", "**/lnInclude/**", "!src/keep.dep"])
        self.assertTrue(r.decide("build/x.C"))
        self.assertTrue(r.decide("src/build/x.C"))                       # unanchored dir matches at any depth
        self.assertTrue(r.decide("tutorials/case/system/controlDict"))
        self.assertIsNone(r.decide("src/tutorials.C"))                    # anchored: only the top-level directory
        self.assertTrue(r.decide("src/a/b.dep"))
        self.assertFalse(r.decide("src/keep.dep"))                        # '!' re-includes
        self.assertTrue(r.decide("src/OpenFOAM/lnInclude/fvMesh.H"))

    def test_micignore_and_include_in_a_repo(self):
        with tempfile.TemporaryDirectory() as repo:
            for rel in ("src/core/Field.H", "src/core/Field.C", "tutorials/cavity/run.C", "applications/solver.C"):
                os.makedirs(os.path.dirname(os.path.join(repo, rel)), exist_ok=True)
                open(os.path.join(repo, rel), "w").write("int f() { return 1; }\n")
            open(os.path.join(repo, ".micignore"), "w").write("tutorials/\n")
            json.dump({"extensions": [".H", ".C"], "include": ["src/**", "applications/**"]},
                      open(os.path.join(repo, ".mic.json"), "w"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            with mock.patch.object(scan, "USER_CONFIG", "/nonexistent"):
                got = sorted(f.path for f in scan.scan(repo))
            self.assertEqual(got, ["applications/solver.C", "src/core/Field.C", "src/core/Field.H"])


OPENFOAM_C = """namespace Foam
{
    defineTypeNameAndDebug(fvMatrix, 0);
}

template<class Type>
template<class Type2>
void Foam::fvMatrix<Type>::addToInternalField
(
    const labelUList& addr,
    Field<Type2>& intf
) const
{
    intf[addr[0]] += 1;
}


template<class Type>
Foam::SolverPerformance<Type> Foam::fvMatrix<Type>::solve
(
    const dictionary& solverControls
)
{
    return solveSegregated(solverControls);
}


template<class Type>
void Foam::fvMatrix<Type>::operator+=(const fvMatrix<Type>& fvmv)
{
    source_ -= fvmv.source_;
}
"""


class OpenFOAMStyle(unittest.TestCase):
    def test_templated_multiline_definitions(self):
        syms = {s["name"]: s for s in symbols.extract(OPENFOAM_C, "cpp")[0]}
        self.assertIn("Foam::fvMatrix::addToInternalField", syms)
        self.assertEqual(syms["Foam::fvMatrix::addToInternalField"]["start"], 6)   # span starts at template<...>
        self.assertIn("Foam::fvMatrix::solve", syms)                            # return type not captured in name
        self.assertIn("Foam::fvMatrix::operator+=", syms)
        self.assertNotIn("Foam", syms)                                           # namespaces are not symbols


REG_C = """namespace Foam
{
    defineTypeNameAndDebug(fixedValue, 0);
    addToRunTimeSelectionTable(distribution, fixedValue, dictionary);
}

makePatchFields(fixedValue);

void Foam::helper::build()
{
    makeSomething(x);
}
"""


class V111(unittest.TestCase):
    def test_registrations_indexed_at_file_level_only(self):
        syms = symbols.extract(REG_C, "cpp")[0]
        regs = [s["name"] for s in syms if s["kind"] == "registration"]
        self.assertEqual(regs, ["defineTypeNameAndDebug(fixedValue, 0)",
                                "addToRunTimeSelectionTable(distribution, fixedValue, dictionary)",
                                "makePatchFields(fixedValue)"])               # the call inside build() is dropped

    def test_config_command_writes_repo_files(self):
        from micode import config_cmd
        with tempfile.TemporaryDirectory() as repo, mock.patch.object(scan, "USER_CONFIG", "/nonexistent"):
            config_cmd.run(repo, "ext", [".tpp=cpp", ".H"])
            config_cmd.run(repo, "ignore", ["tutorials/", "tutorials/"])
            config_cmd.run(repo, "include", ["src/finiteVolume"])
            cfg = json.load(open(os.path.join(repo, ".mic.json")))
            self.assertEqual(cfg["extensions"], {".tpp": "cpp", ".H": "cpp"})
            self.assertEqual(cfg["include"], ["src/finiteVolume/**"])
            self.assertEqual(open(os.path.join(repo, ".micignore")).read(), "tutorials/\n")   # no duplicates

    def test_unknown_extensions_reported(self):
        with tempfile.TemporaryDirectory() as repo:
            for k in range(25):
                open(os.path.join(repo, f"case{k}.foam"), "w").write("x\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            with mock.patch.object(scan, "USER_CONFIG", "/nonexistent"):
                scan.scan(repo)
            self.assertEqual(scan.UNKNOWN.get(".foam"), 25)
