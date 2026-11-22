"""Test only: the sattle.me SDK faked inside pretix, for the plugin's end-to-end test (e2e/).

Installed beside pretix-sattleme in that test's pretix alone, never shipped: when pretix starts,
it replaces the one seam through which the plugin makes its clients (`pretix_sattleme.sdk.Client`)
with a fake (`fake.FakeClient`), and adds the address through which the test pays an invoice
(`urls.py`). The plugin itself has no switch for it.
"""
