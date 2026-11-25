import json
import re

from .tools import READ_ONLY_TOOLS, TOOLS


class Improvements:
    def __init__(self, store):
        self.store = store

    def rows(self, account):
        return self.store.all("SELECT * FROM improvements WHERE user_id=? ORDER BY id DESC LIMIT 100", (account,))

    def entry(self, account, identifier):
        row = self.store.one("SELECT * FROM improvements WHERE user_id=? AND id=?", (account, identifier))
        if not row:
            raise FileNotFoundError("Improvement proposal not found")
        row["proposal"] = json.loads(row["proposal"])
        return row

    def propose(self, account, name, kind, proposal):
        if kind == "workflow":
            self.steps(proposal)
        elif kind == "python":
            source = proposal.get("source", "")
            if not isinstance(source, str) or not source.strip() or len(source) > 40000:
                raise ValueError("Provide Python source (up to 40,000 characters)")
        else:
            raise ValueError("Unknown improvement type")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT coalesce(max(version),0)+1 FROM improvements WHERE user_id=? "
                                 "AND name=?", (account, name)).fetchone()[0]
            return db.execute(
                "INSERT INTO improvements(user_id,name,kind,proposal,state,version) VALUES (?,?,?,?,'proposed',?)",
                (account, name, kind, json.dumps(proposal), version),
            ).lastrowid

    @staticmethod
    def steps(proposal):
        steps = proposal.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            raise ValueError("A workflow requires one to eight steps")
        for step in steps:
            if set(step) != {"tool", "arguments", "expect"} or step["tool"] not in READ_ONLY_TOOLS:
                raise ValueError("A workflow can use only supported read-only tools and must specify expected results")
            TOOLS[step["tool"]].schema.model_validate(step["arguments"])
            if not isinstance(step["expect"], dict) or not step["expect"]:
                raise ValueError("Expected results must map fields to exact values")
        return steps

    @staticmethod
    def assert_result(result, expect):
        for field, value in expect.items():
            if not isinstance(field, str) or not re.fullmatch(r"[\w.]{1,120}", field):
                raise ValueError("Invalid result field")
            actual = result
            for section in field.split("."):
                if not isinstance(actual, dict) or section not in actual:
                    raise ValueError(f"Missing result field {field}")
                actual = actual[section]
            if actual != value:
                raise ValueError(f"Expected {field}={value!r}, got {actual!r}")

    async def trial(self, run, executor, proposal):
        evidence = []
        for step in self.steps(proposal):
            name = step["tool"]
            data = executor.prepare(run, name, step["arguments"], executor.available(run, READ_ONLY_TOOLS))
            result = await executor.perform(run, name, data)
            self.assert_result(result, step["expect"])
            evidence.append({"tool": name, "verified": step["expect"]})
        return evidence

    async def validate(self, run, executor, identifier):
        row = self.entry(run.account, identifier)
        try:
            if row["kind"] == "python":
                compile(row["proposal"]["source"], "<reviewed source proposal>", "exec")
                self.store.run("UPDATE improvements SET state='review',evidence=? WHERE id=?",
                               ("Python syntax verified. Execution and code review are required before deployment.",
                                identifier))
            else:
                evidence = await self.trial(run, executor, row["proposal"])
                self.store.run("UPDATE improvements SET state='validated',evidence=? WHERE id=?",
                               (json.dumps(evidence), identifier))
        except Exception as error:
            self.store.run("UPDATE improvements SET state='rejected',evidence=? WHERE id=?",
                           (str(error), identifier))
            raise
        return self.entry(run.account, identifier)

    def activate(self, account, identifier):
        row = self.entry(account, identifier)
        if row["kind"] != "workflow" or row["state"] not in {"validated", "retired", "active"}:
            raise ValueError("Validate the workflow before enabling it")
        with self.store.connect() as db:
            db.execute("UPDATE improvements SET state='retired' WHERE user_id=? AND name=? AND state='active'",
                       (account, row["name"]))
            db.execute("UPDATE improvements SET state='active' WHERE id=?", (identifier,))
        return self.entry(account, identifier)

    async def run(self, run, executor, identifier):
        row = self.entry(run.account, identifier)
        if row["state"] != "active":
            raise PermissionError("The workflow is not enabled")
        evidence = await self.trial(run, executor, row["proposal"])
        return {"workflow": row["name"], "version": row["version"], "steps": evidence, "verified": True}
