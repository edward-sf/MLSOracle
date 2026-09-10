from main import main


def test_main_runs(capsys):
    main()
    captured = capsys.readouterr()
    assert "Hello from MLS Oracle!" in captured.out
