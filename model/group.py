from core.capabilities import Capabilities
from model.user import User
from sqlalchemy.dialects.postgresql.array import ARRAY
from . import db


class Group(db.Model):
    __tablename__ = 'group'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(32), index=True, unique=True, nullable=False)
    capabilities = db.Column('capabilities', ARRAY(db.Text), nullable=False, server_default='{}')
    private = db.Column(db.Boolean)

    @property
    def pending_group(self):
        return self.private and db.session.query(User).filter(User.login == self.name).first().pending

    @property
    def immutable(self):
        return self.private or self.name == "public"

    @property
    def user_logins(self):
        return [ug.login for ug in self.users]

    @staticmethod
    def public_group():
        return Group.get_by_name("public")

    @staticmethod
    def get_by_name(name):
        return db.session.query(Group).filter(Group.name == name).first()

    @staticmethod
    def all_access_groups():
        return db.session.query(Group) \
            .filter(Group.capabilities.contains([Capabilities.access_all_objects])).all()
