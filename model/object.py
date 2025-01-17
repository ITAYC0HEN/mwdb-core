import datetime
import os

from flask import g
from sqlalchemy import and_, exists, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, contains_eager
from sqlalchemy.sql.expression import true

from core.capabilities import Capabilities
from core.util import get_sample_path

from . import db
from .metakey import Metakey, MetakeyDefinition, MetakeyPermission
from .tag import object_tag_table, Tag

relation = db.Table(
    'relation',
    db.Column('parent_id', db.Integer, db.ForeignKey('object.id'), index=True),
    db.Column('child_id', db.Integer, db.ForeignKey('object.id'), index=True),
    db.Column('creation_time', db.DateTime, default=datetime.datetime.now),
    db.Index('ix_relation_parent_child', 'parent_id', 'child_id', unique=True)
)


class AccessType:
    ADDED = "added"
    SHARED = "shared"
    QUERIED = "queried"
    MIGRATED = "migrated"


class ObjectPermission(db.Model):
    __tablename__ = 'permission'

    object_id = db.Column(db.Integer, db.ForeignKey('object.id'), primary_key=True, autoincrement=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), primary_key=True, autoincrement=False, index=True)
    __table_args__ = (
        db.Index('ix_permission_group_object', 'object_id', 'group_id', unique=True),
    )

    access_time = db.Column(db.DateTime, nullable=False, index=True, default=func.now())

    reason_type = db.Column(db.String(32))
    related_object_id = db.Column(db.Integer, db.ForeignKey('object.id'))
    related_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)

    object = db.relationship('Object', foreign_keys=[object_id], lazy='joined',
                             back_populates="shares")
    related_object = db.relationship('Object', foreign_keys=[related_object_id], lazy='joined',
                                     back_populates="related_shares")
    related_user = db.relationship('User', foreign_keys=[related_user_id], lazy='joined')

    group = db.relationship('Group', foreign_keys=[group_id], lazy='joined')

    @property
    def access_reason(self):
        """TODO: This is just for backwards compatibility, remove that part in further major release"""
        if self.reason_type == "migrated":
            return "Migrated from mwdbv1"
        return "{reason_type} {related_object_type}:{related_object_dhash} by user:{related_user_login}".format(
            reason_type=self.reason_type,
            related_object_type=self.related_object_type,
            related_object_dhash=self.related_object_dhash,
            related_user_login=self.related_user_login
        )

    @property
    def group_name(self):
        return self.group.name

    @property
    def related_object_dhash(self):
        return self.related_object.dhash

    @property
    def related_object_type(self):
        return self.related_object.type

    @property
    def related_user_login(self):
        return self.related_user.login

    @classmethod
    def create(cls, object_id, group_id, reason_type, related_object, related_user):
        if not db.session.query(exists().where(
                and_(
                    ObjectPermission.object_id == object_id,
                    ObjectPermission.group_id == group_id
                )
        )).scalar():
            try:
                perm = ObjectPermission(object_id=object_id,
                                        group_id=group_id,
                                        reason_type=reason_type,
                                        related_object=related_object,
                                        related_user=related_user)
                db.session.add(perm)
                db.session.flush()
                # Capabilities were created right now
                return True
            except IntegrityError:
                db.session.rollback()
                if not db.session.query(exists().where(
                        and_(
                            ObjectPermission.object_id == object_id,
                            ObjectPermission.group_id == group_id
                        )
                )).scalar():
                    raise
        # Capabilities exist yet
        return False


class Object(db.Model):
    __tablename__ = 'object'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    type = db.Column(db.String(50))
    dhash = db.Column(db.String(64), unique=True, index=True)
    upload_time = db.Column(db.DateTime, nullable=False, index=True, default=func.now())

    parents = db.relationship(
        "Object", secondary=relation,
        primaryjoin=(id == relation.c.child_id),
        secondaryjoin=(id == relation.c.parent_id),
        order_by=relation.c.creation_time.desc(),
        backref=db.backref('children', order_by=relation.c.creation_time.desc()))

    meta = db.relationship('Metakey', backref='object', lazy=True)
    comments = db.relationship('Comment', backref='object', lazy='dynamic')
    tags = db.relationship('Tag', secondary=object_tag_table, back_populates='objects', lazy='joined')

    shares = db.relationship('ObjectPermission', lazy='dynamic',
                             foreign_keys=[ObjectPermission.object_id],
                             back_populates="object")
    related_shares = db.relationship('ObjectPermission', lazy='dynamic',
                                     foreign_keys=[ObjectPermission.related_object_id],
                                     back_populates="related_object")

    @property
    def latest_config(self):
        from .config import Config
        return (
            db.session.query(Config)
                      .filter(Config.id.in_(db.session.query(relation.c.child_id)
                                                      .filter(relation.c.parent_id == self.id)))
                      .filter(g.auth_user.has_access_to_object(Config.id)).first())

    def add_parent(self, parent, commit=True):
        """
        Adding parent with permission inheritance
        """
        if parent in self.parents:
            return False
        self.parents.append(parent)
        permissions = db.session.query(ObjectPermission) \
            .filter(ObjectPermission.object_id == parent.id).all()
        for perm in permissions:
            self.give_access(perm.group_id, perm.reason_type, perm.related_object, perm.related_user, commit=commit)
        return True

    def give_access(self, group_id, reason_type, related_object, related_user, commit=True):
        """
        Give access to group with recursive propagation
        """
        visited = set()
        queue = [self]
        while len(queue):
            obj = queue.pop(0)
            if obj.id in visited:
                continue
            visited.add(obj.id)
            if ObjectPermission.create(obj.id, group_id, reason_type, related_object, related_user):
                """
                If permission hasn't exist earlier - continue propagation
                """
                queue += obj.children

        if commit:
            db.session.commit()

    def has_explicit_access(self, user):
        """
        Check whether user has access via explicit ObjectPermissions
        Used by Object.access
        """
        return db.session.query(
            exists().where(and_(
                ObjectPermission.object_id == self.id,
                user.is_member(ObjectPermission.group_id)
            ))).scalar()

    @classmethod
    def get(cls, identifier):
        """
        Polymorphic getter for object via specified identifier (provided by API) without access check-ups.
        Don't include internal (sequential) identifiers in filtering!
        Used by Object.access
        """
        return cls.query.filter(cls.dhash == identifier)

    @classmethod
    def get_or_create(cls, obj, file=None):
        """
        Polymophic get or create pattern, useful in dealing with race condition resulting in IntegrityError
        on the dhash unique constraint.
        Pattern from here - http://rachbelaid.com/handling-race-condition-insert-with-sqlalchemy/
        Returns tuple with object and boolean value if new object was created or not, True == new object
        """
        is_new = False
        new_cls = Object.get(obj.dhash).first()
        if new_cls is not None:
            new_cls = cls.get(obj.dhash).first()
            return new_cls, is_new

        db.session.begin_nested()

        new_cls = obj
        try:
            db.session.add(new_cls)
            db.session.flush()

            if file is not None:
                file.stream.seek(0, os.SEEK_SET)
                file.save(get_sample_path(obj.dhash))

            db.session.commit()
            is_new = True
        except IntegrityError:
            db.session.rollback()
            new_cls = cls.get(obj.dhash).first()
            if new_cls is None:
                raise
        return new_cls, is_new

    @classmethod
    def access(cls, identifier, requestor):
        from .group import Group
        """
        Gets object with specified identifier including requestor rights.
        Shouldn't be used directly in Resources definition (use authenticated_access wrapper)
        Returns None when user has no rights to specified object or object doesn't exist
        """
        obj = cls.get(identifier)
        # If object doesn't exist - it doesn't exist
        if obj.first() is None:
            return None

        """
        In that case we want only those parents to which requestor has access.
        """
        stmtp = (
            db.session.query(Object)
                      .filter(Object.id.in_(
                          db.session.query(relation.c.parent_id)
                                    .filter(relation.c.child_id == obj.first().id)))
                      .filter(requestor.has_access_to_object(Object.id))
        )
        stmtp = stmtp.subquery()

        parent = aliased(Object, stmtp)

        obj = obj.outerjoin(parent, Object.parents) \
            .options(contains_eager(Object.parents, alias=parent)).all()[0]

        # Ok, now let's check whether requestor has explicit access
        if obj.has_explicit_access(requestor):
            return obj

        # If not, but has "share_queried_objects" rights: that's good moment to give_access
        if requestor.has_rights(Capabilities.share_queried_objects):
            share_queried_groups = db.session.query(Group).filter(
                and_(
                    Group.capabilities.contains([Capabilities.share_queried_objects]),
                    requestor.is_member(Group.id)
                )
            ).all()
            for group in share_queried_groups:
                obj.give_access(group.id, AccessType.QUERIED, obj, requestor)
            return obj
        # Well.. I've tried
        return None

    def get_tags(self):
        """
        Get object tags
        :return: List of strings representing tags
        """
        return [tag.tag for tag in db.session.query(Tag).filter(Tag.objects.any(id=self.id)).all()]

    def add_tag(self, tag_name):
        """
        Adds new tag to object.
        :param tag_name: tag string
        :return: True if tag wasn't added yet
        """
        db_tag = Tag()
        db_tag.tag = tag_name
        db_tag, is_new_tag = Tag.get_or_create(db_tag)

        try:
            if self not in db_tag.objects:
                db_tag.objects.append(self)
                db.session.add(db_tag)
                db.session.flush()
                db.session.commit()
                return True
        except IntegrityError:
            db.session.refresh(db_tag)
            if self not in db_tag.objects:
                raise
        return False

    def remove_tag(self, tag_name):
        """
        Removes tag from object
        :param tag_name: tag string
        :return: True if tag wasn't removed yet
        """
        db_tag = db.session.query(Tag).filter(tag_name == Tag.tag)
        if db_tag.scalar() is None:
            return False
        else:
            db_tag = db_tag.one()

        try:
            if self in db_tag.objects:
                db_tag.objects.remove(self)
                db.session.add(db_tag)
                db.session.flush()
                db.session.commit()
                return True
        except IntegrityError:
            db.session.refresh(db_tag)
            if self in db_tag.objects:
                raise
        return False

    def get_metakeys(self, as_dict=False, check_permissions=True, show_hidden=False):
        """
        Gets all object metakeys (attributes)
        :param as_dict: Return dict object instead of list of Metakey objects (default: False)
        :param check_permissions: Filter results including current user permissions (default: True)
        :param show_hidden: Show hidden metakeys
        """
        metakeys = db.session.query(Metakey) \
                             .filter(Metakey.object_id == self.id) \
                             .join(MetakeyDefinition, MetakeyDefinition.key == Metakey.key)

        if check_permissions and not g.auth_user.has_rights(Capabilities.reading_all_attributes):
            metakeys = metakeys.filter(
                Metakey.key.in_(
                    db.session.query(MetakeyPermission.key)
                              .filter(MetakeyPermission.can_read == true())
                              .filter(g.auth_user.is_member(MetakeyPermission.group_id))))

        if not show_hidden:
            metakeys = metakeys.filter(MetakeyDefinition.hidden.is_(False))

        metakeys = metakeys.order_by(Metakey.id).all()

        if not as_dict:
            return metakeys

        dict_metakeys = {}
        for metakey in metakeys:
            if metakey.key not in dict_metakeys:
                dict_metakeys[metakey.key] = []
            dict_metakeys[metakey.key].append(metakey.value)
        return dict_metakeys

    def add_metakey(self, key, value, commit=True, check_permissions=True):
        metakey_definition = db.session.query(MetakeyDefinition) \
                                       .filter(MetakeyDefinition.key == key).first()
        if not metakey_definition:
            # Attribute needs to be defined first
            return None

        if check_permissions and not g.auth_user.has_rights(Capabilities.adding_all_attributes):
            metakey_permission = db.session.query(MetakeyPermission) \
                .filter(MetakeyPermission.key == key) \
                .filter(MetakeyPermission.can_set == true()) \
                .filter(g.auth_user.is_member(MetakeyPermission.group_id)).first()
            if not metakey_permission:
                # Nope, you don't have permission to set that metakey!
                return None

        db_metakey = Metakey(key=key, value=value, object_id=self.id)
        _, is_new = Metakey.get_or_create(db_metakey)
        if commit:
            db.session.commit()
        return is_new

    __mapper_args__ = {
        'polymorphic_identity': __tablename__,
        'polymorphic_on': type
    }
