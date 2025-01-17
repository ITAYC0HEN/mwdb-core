import datetime
import os
import bcrypt

from flask import current_app as app
from itsdangerous import TimedJSONWebSignatureSerializer, SignatureExpired, BadSignature
from sqlalchemy import and_

from sqlalchemy.orm.exc import NoResultFound

from core.config import app_config

from . import db
from .object import ObjectPermission


member = db.Table(
    'member', db.metadata,
    db.Column('user_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('group_id', db.Integer, db.ForeignKey('group.id'))
)


class User(db.Model):
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    login = db.Column(db.String(32), index=True, unique=True, nullable=False)
    email = db.Column(db.String(128))

    password_hash = db.Column(db.String(128))
    # Legacy "version_uid", todo: remove it when users are ready
    version_uid = db.Column(db.String(16))
    # Password version (set password link and session token validation)
    # Invalidates set password link or session when password has been changes
    password_ver = db.Column(db.String(16))
    # Identity version (session token validation)
    # Invalidates session when user capabilities has been changed
    identity_ver = db.Column(db.String(16))

    additional_info = db.Column(db.String)
    disabled = db.Column(db.Boolean, default=False, nullable=False)
    pending = db.Column(db.Boolean, default=False, nullable=False)

    requested_on = db.Column(db.DateTime)
    registered_on = db.Column(db.DateTime)
    registered_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    logged_on = db.Column(db.DateTime)
    set_password_on = db.Column(db.DateTime)

    groups = db.relationship('Group', secondary=member, backref='users', lazy='selectin')
    comments = db.relationship('Comment', backref='user')
    api_keys = db.relationship('APIKey', foreign_keys="APIKey.user_id", backref='user')
    registrar = db.relationship('User', foreign_keys="User.registered_by", remote_side=[id], uselist=False)

    # used to load-balance the malware processing pipeline
    feed_quality = db.Column(db.String(32), server_default='high')

    @property
    def group_names(self):
        return [group.name for group in self.groups]

    @property
    def registrar_login(self):
        return self.registrar and self.registrar.login

    @property
    def capabilities(self):
        return set.union(*[set(group.capabilities) for group in self.groups])

    def has_rights(self, perms):
        return perms in self.capabilities

    def set_password(self, password):
        self.password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(12)).decode('utf-8')
        self.password_ver = os.urandom(8).hex()
        self.set_password_on = datetime.datetime.now()

    def reset_sessions(self):
        # Should be also called for fresh user objects
        self.identity_ver = os.urandom(8).hex()

    def verify_password(self, password):
        if self.password_hash is None:
            return False
        return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))

    def _generate_token(self, fields, expiration):
        s = TimedJSONWebSignatureSerializer(app_config.malwarecage.secret_key, expires_in=expiration)
        return s.dumps(dict([('login', self.login)] + [(field, getattr(self, field)) for field in fields]))

    @staticmethod
    def _verify_token(token, fields):
        s = TimedJSONWebSignatureSerializer(app_config.malwarecage.secret_key)
        try:
            data = s.loads(token)
        except SignatureExpired:
            return None
        except BadSignature:
            return None

        try:
            user_obj = User.query.filter(User.login == data['login']).one()
        except NoResultFound:
            return None

        for field in fields:
            if field not in data:
                return None
            if data[field] != getattr(user_obj, field):
                return None

        return user_obj

    def generate_session_token(self):
        return self._generate_token(["password_ver", "identity_ver"], expiration=24 * 3600)

    def generate_set_password_token(self):
        return self._generate_token(["password_ver"], expiration=14 * 24 * 3600)

    @staticmethod
    def verify_session_token(token):
        return User._verify_token(token, ["password_ver", "identity_ver"])

    @staticmethod
    def verify_set_password_token(token):
        return User._verify_token(token, ["password_ver"])

    @staticmethod
    def verify_legacy_token(token):
        return User._verify_token(token, ["version_uid"])

    def is_member(self, group_id):
        groups = db.session.query(member.c.group_id) \
            .filter(member.c.user_id == self.id)
        return group_id.in_(groups)

    def has_access_to_object(self, object_id):
        return object_id.in_(db.session.query(ObjectPermission.object_id)
                             .filter(self.is_member(ObjectPermission.group_id)))

    def has_uploaded_object(self, object_id):
        return object_id.in_(db.session.query(ObjectPermission.object_id)
                             .filter(and_(ObjectPermission.related_object == ObjectPermission.object_id,
                                          ObjectPermission.related_user_id == self.id)))
