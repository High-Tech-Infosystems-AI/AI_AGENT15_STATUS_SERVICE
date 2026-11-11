from app.database_Layer.db_config import masked_db_uri


def test_password_is_masked():
    out = masked_db_uri("mysql+mysqlconnector://hrmis-hti:s3cr%40t@db.local:3306/ats_staging")
    assert "s3cr" not in out and "%40t" not in out
    assert out == "mysql+mysqlconnector://hrmis-hti:***@db.local:3306/ats_staging"


def test_uri_without_password_is_unchanged():
    assert masked_db_uri("sqlite:///./test.db") == "sqlite:///./test.db"


def test_unparseable_uri_is_not_echoed():
    assert masked_db_uri("not a url with secret") == "<unparseable database URI>"
