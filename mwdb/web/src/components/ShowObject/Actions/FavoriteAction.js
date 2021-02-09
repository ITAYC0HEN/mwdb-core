import React, { useContext } from "react";

import { faStar } from "@fortawesome/free-solid-svg-icons";
import { faStar as farStar } from "@fortawesome/free-regular-svg-icons";

import api from "@mwdb-web/commons/api";
import { ObjectContext } from "@mwdb-web/commons/context";
import { ObjectAction } from "@mwdb-web/commons/ui";

export default function FavoriteAction(props) {
    const context = useContext(ObjectContext);

    async function markFavoriteObject() {
        try {
            await api.addObjectFavorite(context.object.id);
            context.updateObject();
        } catch (error) {
            context.setObjectError(error);
        }
    }

    async function unmarkFavoriteObject() {
        try {
            await api.removeObjectFavorite(context.object.id);
            context.updateObject();
        } catch (error) {
            context.setObjectError(error);
        }
    }

    if (context.object.favorite)
        return (
            <ObjectAction
                label="Unfavorite"
                icon={faStar}
                action={() => unmarkFavoriteObject()}
            />
        );
    else
        return (
            <ObjectAction
                label="Favorite"
                icon={farStar}
                action={() => markFavoriteObject()}
            />
        );
}