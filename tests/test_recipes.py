from hashlib import sha256
from html.parser import HTMLParser
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import tempfile
import unittest

from lightbrain.recipes import catalog, render


class RecipeTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/".runtime"/"recipe-tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root,ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_composition_renders_both_pinned_recipes_and_runs_storage(self):
        pins=catalog()
        result=render(pins,self.root)
        self.assertEqual(len(result["receipts"]),2)
        self.assertEqual(list(self.root.iterdir()),[])
        for receipt in result["receipts"]:
            self.assertEqual(receipt["engine"],"copier")
            for name, digest in receipt["outputs"].items():
                self.assertEqual(sha256(result["files"][name].encode()).hexdigest(),digest)
        module_file=self.root/"local_records.py"
        module_file.write_bytes(result["files"]["local_records.py"].encode())
        spec=spec_from_file_location("delivered_records",module_file)
        storage=module_from_spec(spec)
        spec.loader.exec_module(storage)
        db=self.root/"project.sqlite"
        notes=storage.Records(db,"notes")
        second=storage.Records(db,"notes")
        assigned=notes.create({"title":"First"})
        self.assertEqual(second.get(assigned["id"]),assigned)
        changed=second.update(assigned["id"],1,{"title":"Changed"})
        self.assertEqual(changed["revision"],2)
        with self.assertRaises(storage.RevisionConflict):
            notes.update(assigned["id"],1,{"title":"Stale update"})
        self.assertEqual(storage.Records(db,"other").list(),[])
        notes.delete(assigned["id"],2)
        self.assertIsNone(second.get(assigned["id"]))

        class Tags(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags=[]
            def handle_starttag(self,tag,attrs):
                self.tags.append(tag)
        tags=Tags()
        tags.feed(result["files"]["components/disclosure.html"])
        self.assertIn("details",tags.tags)
        self.assertIn("summary",tags.tags)
        self.assertIn("prefers-reduced-motion",result["files"]["components/disclosure.css"])

    def test_wrong_digest_or_conflicting_existing_source_is_rejected(self):
        pin=next(r for r in catalog() if r["id"]=="local-records")
        with self.assertRaises(ValueError):
            render([{**pin,"digest":"0"*64}],self.root)
        with self.assertRaises(ValueError):
            render([pin,pin],self.root)
        source=self.root/"local_records.py"
        source.write_text("user content",encoding="utf-8")
        with self.assertRaises(ValueError):
            render([pin],self.root)
        self.assertEqual(source.read_text(encoding="utf-8"),"user content")

    @unittest.skipUnless(os.environ.get("LB_TEST_BROWSER"),"set LB_TEST_BROWSER to a Chromium binary")
    def test_keyboard_mobile_and_reduced_motion_in_browser(self):
        from playwright.sync_api import sync_playwright
        pin=next(r for r in catalog() if r["id"]=="disclosure-panel")
        files=render([pin],self.root)["files"]
        css=files["components/disclosure.css"]
        panel=files["components/disclosure.html"].replace('<link rel="stylesheet" href="/components/disclosure.css">',
                                                          "<style>"+css+"</style>")
        pagefile=self.root/"screen.html"
        pagefile.write_text('<meta name="viewport" content="width=device-width">'+panel,encoding="utf-8")
        with sync_playwright() as pw:
            ctx=pw.chromium.launch_persistent_context(
                str(self.root/"browser-profile"),executable_path=os.environ["LB_TEST_BROWSER"],headless=True,
                args=["--disable-crash-reporter","--disable-breakpad","--disable-background-networking",
                      "--disk-cache-dir="+str(self.root/"browser-cache")],
                viewport={"width":320,"height":640},reduced_motion="reduce")
            try:
                page=ctx.new_page()
                page.goto(pagefile.as_uri())
                page.keyboard.press("Tab")
                self.assertTrue(page.locator("summary").evaluate("x => x === document.activeElement"))
                page.keyboard.press("Enter")
                self.assertTrue(page.locator("details").evaluate("x => x.open"))
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                self.assertEqual(page.locator(".lb-disclosure__chevron").evaluate(
                    "x => getComputedStyle(x).transitionDuration"),"0s")
                page.keyboard.press("Space")
                self.assertFalse(page.locator("details").evaluate("x => x.open"))
            finally:
                ctx.close()
