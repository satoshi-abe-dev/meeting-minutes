"""presenter — the screen-logic (Presenter) layer. This folder = this namespace.

The Presenter depends only on the View's contract
(``meeting_minutes.view.MainView``) and the Model
(``meeting_minutes.model.config`` / ``meeting_minutes.model.pipeline``,
etc.), and knows nothing about tkinter at all. Unit tests are done by
plugging in a FakeView.
"""
