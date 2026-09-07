"""presenter — 画面ロジック（Presenter）層。フォルダ ＝ この名前空間。

Presenter は View の契約（``meeting_minutes.view.MainView``）と Model
（``meeting_minutes.model.config`` / ``meeting_minutes.model.pipeline`` ほか）にだけ依存し、
tkinter を一切知らない。単体テストは FakeView を差し込んで行う。
"""
