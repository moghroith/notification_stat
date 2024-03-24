from collections import defaultdict, Counter
import streamlit as st
import requests
import pandas as pd
import numpy as np
import time

API_URL = "https://api.yodayo.com/v1/notifications"
LIMIT = 500


def authenticate_with_token(access_token):
    session = requests.Session()
    jar = requests.cookies.RequestsCookieJar()
    jar.set("access_token", access_token)
    session.cookies = jar
    return session


def process_liked_notification(notification, user_likes):
    name = notification["user_profile"]["name"]
    resource_uuid = notification["resource_uuid"]
    created_at = notification["created_at"]

    user_likes[name][(resource_uuid, created_at)] += 1


def process_commented_notification(notification, user_comments, resource_comments):
    name = notification["user_profile"]["name"]
    resource_uuid = notification["resource_uuid"]

    user_comments[name] += 1
    resource_comments[resource_uuid] += 1


def process_collected_notification(notification, resource_collected):
    resource_uuid = notification["resource_uuid"]
    resource_collected[resource_uuid] += 1


@st.cache_data(ttl=7200)
def generate_likes_dataframe(user_likes):
    liked_data = [
        (user, resource_uuid, created_at, count)
        for user, liked_posts in user_likes.items()
        for (resource_uuid, created_at), count in liked_posts.items()
    ]

    likes_df = pd.DataFrame(
        liked_data, columns=["actor_uuid", "resource_uuid", "created_at", "count"]
    )
    likes_df = likes_df.explode("count").reset_index(drop=True)
    likes_df["created_at"] = pd.to_datetime(likes_df["created_at"])
    likes_df = likes_df.sort_values(by="created_at", ascending=False)
    likes_df["resource_uuid"] = "https://yodayo.com/posts/" + likes_df["resource_uuid"]

    return likes_df


@st.cache_data(ttl=7200)
def generate_comments_dataframe(user_comments, user_is_follower, notifications):
    comments_data = [
        {
            "actor_uuid": notification["user_profile"]["name"],
            "resource_uuid": notification["resource_uuid"],
            "created_at": notification["created_at"],
            "is_follower": user_is_follower[notification["user_profile"]["name"]],
        }
        for notification in notifications
        if notification["action"] == "commented"
    ]

    comments_df = pd.DataFrame(comments_data)
    comments_df["created_at"] = pd.to_datetime(comments_df["created_at"])
    comments_df = comments_df.sort_values(by="created_at", ascending=False)
    comments_df["resource_uuid"] = (
        "https://yodayo.com/posts/" + comments_df["resource_uuid"]
    )
    return comments_df


@st.cache_data(ttl=7200)
def get_followers(_session, user_id):
    followers = []
    offset = 0
    limit = 500
    while True:
        followers_url = f"https://api.yodayo.com/v1/users/{user_id}/followers"
        params = {"offset": offset, "limit": limit, "width": 600, "include_nsfw": True}
        resp = _session.get(followers_url, params=params)
        follower_data = resp.json()
        followers.extend([user["profile"]["name"] for user in follower_data["users"]])
        if len(follower_data["users"]) < limit:
            break
        offset += limit
    return followers


@st.cache_data(ttl=7200)
def analyze_likes(user_likes, followers, follower_like_counts):
    likes_df = generate_likes_dataframe(user_likes)
    follower_names = set(followers)
    users_with_likes = set(likes_df["actor_uuid"].unique())
    followers_no_likes = list(follower_names - users_with_likes)
    users_with_no_likes_count = len(followers_no_likes)
    total_followers = len(follower_names)
    st.write(f"Followers who didn't leave any likes: {followers_no_likes}")
    st.write(
        f"{users_with_no_likes_count} ({users_with_no_likes_count/total_followers*100:.2f}%) out of {total_followers} followers didn't leave any likes"
    )

    likes_by_followers = likes_df[likes_df["actor_uuid"].isin(follower_names)].shape[0]
    likes_by_non_followers = likes_df[
        ~likes_df["actor_uuid"].isin(follower_names)
    ].shape[0]
    total_likes = likes_by_followers + likes_by_non_followers

    st.write(
        f"Likes by followers: {likes_by_followers} ({likes_by_followers/total_likes*100:.2f}%)"
    )
    st.write(
        f"Likes by non-followers: {likes_by_non_followers} ({likes_by_non_followers/total_likes*100:.2f}%)"
    )

    follower_like_counts_series = pd.Series(follower_like_counts)
    follower_like_counts_df = follower_like_counts_series[
        follower_like_counts_series.index.isin(follower_names)
    ].reset_index()
    follower_like_counts_df.columns = ["follower", "likes"]
    follower_like_counts_df = follower_like_counts_df[
        follower_like_counts_df["likes"] > 0
    ]

    non_follower_like_counts_df = (
        likes_df[~likes_df["actor_uuid"].isin(follower_names)]["actor_uuid"]
        .value_counts()
        .reset_index()
    )
    non_follower_like_counts_df.columns = ["actor", "likes"]
    col1, col2 = st.columns(2)

    with col1:
        st.subheader(
            "Distribution of Likes by Followers",
            help="Shows what № of followers left what amount of likes and their percentage out of total amount of followers",
        )
        follower_likes_summary = (
            follower_like_counts_df.groupby("likes")["follower"].count().reset_index()
        )
        follower_likes_summary.columns = ["likes", "count"]
        follower_likes_summary["percentage"] = (
            follower_likes_summary["count"] / total_followers
        ) * 100
        st.dataframe(follower_likes_summary, hide_index=True)

    with col2:
        st.subheader(
            "Distribution of Likes by Non-Followers",
            help="Shows what № of non-followers left what amount of likes and their percentage out of total amount of followers",
        )
        non_follower_likes_summary = (
            non_follower_like_counts_df.groupby("likes")["actor"].count().reset_index()
        )
        non_follower_likes_summary.columns = ["likes", "count"]
        non_follower_likes_summary["percentage"] = (
            non_follower_likes_summary["count"]
            / (len(users_with_likes) - total_followers)
        ) * 100
        st.dataframe(non_follower_likes_summary, hide_index=True)


@st.cache_data(ttl=7200)
def load_data(_session, followers):
    offset = 0
    user_likes = defaultdict(Counter)
    user_comments = Counter()
    resource_comments = Counter()
    resource_collected = Counter()
    follower_like_counts = Counter()
    user_is_follower = defaultdict(bool)
    notifications = []

    for follower in followers:
        user_is_follower[follower] = True

    while True:
        resp = _session.get(API_URL, params={"offset": offset, "limit": LIMIT})
        data = resp.json()

        notifications.extend(data.get("notifications", []))

        liked_notifications = [
            n
            for n in data.get("notifications", [])
            if n["action"] == "liked" and n.get("resource_media")
        ]
        commented_notifications = [
            n for n in data.get("notifications", []) if n["action"] == "commented"
        ]
        collected_notifications = [
            n for n in data.get("notifications", []) if n["action"] == "collected"
        ]

        for notification in liked_notifications:
            process_liked_notification(notification, user_likes)
            name = notification["user_profile"]["name"]
            follower_like_counts[name] += 1

        for notification in commented_notifications:
            process_commented_notification(
                notification, user_comments, resource_comments
            )

        for notification in collected_notifications:
            process_collected_notification(notification, resource_collected)

        if len(data.get("notifications", [])) < LIMIT:
            break

        offset += LIMIT

    return (
        user_likes,
        user_comments,
        resource_comments,
        resource_collected,
        follower_like_counts,
        user_is_follower,
        notifications,
    )


def display_top_users_stats(likes_df, percentile, total_likes):
    top_users = likes_df.sort_values("Likes", ascending=False).head(
        int(percentile * len(likes_df))
    )
    pct_top_users = len(top_users) / len(likes_df) * 100
    pct_likes_top_users = top_users["Likes"].sum() / total_likes * 100
    st.write(
        f"{len(top_users)} users ({pct_top_users:.1f}% of all users) contributed {pct_likes_top_users:.1f}% of total likes"
    )

def get_column_config():
    return {
        "actor_uuid": st.column_config.TextColumn("Name"),
        "resource_uuid": st.column_config.LinkColumn(
            "Link", display_text="https://yodayo\.com/posts/(.*?)/"
        ),
        "Resource UUID": st.column_config.LinkColumn(
            "Link", display_text="https://yodayo\.com/posts/(.*?)/"
        ),
    }


def main():
    access_token = st.text_input("Enter your access token")
    user_id = st.text_input("Enter user ID")
    column_config = get_column_config()
    if access_token and user_id:
        session = authenticate_with_token(access_token)
        followers = get_followers(session, user_id)
        start_time = time.perf_counter()
        (
            user_likes,
            user_comments,
            resource_comments,
            resource_collected,
            follower_like_counts,
            user_is_follower,
            notifications,
        ) = load_data(session, followers)

        total_likes = sum(len(posts) for posts in user_likes.values())
        total_comments = sum(user_comments.values())
        st.subheader("Total Likes and Comments")
        st.write(f"Total Likes: {total_likes}")
        st.write(f"Total Comments: {total_comments}")
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Likes by user:")
            likes_df = pd.DataFrame(
                {
                    "User": list(user_likes.keys()),
                    "Likes": [sum(counter.values()) for counter in user_likes.values()],
                    "is_follower": [
                        user_is_follower[user] for user in user_likes.keys()
                    ],
                }
            )
            likes_df = likes_df.sort_values(by="Likes", ascending=False)
            st.dataframe(likes_df, hide_index=True)

        with col2:
            st.subheader("Comments by user:")
            comments_df = pd.DataFrame(
                {
                    "User": list(user_comments.keys()),
                    "Comments": list(user_comments.values()),
                    "is_follower": [
                        user_is_follower[user] for user in user_comments.keys()
                    ],
                }
            )
            comments_df = comments_df.sort_values(by="Comments", ascending=False)
            st.dataframe(comments_df, hide_index=True)

        col3 = st.columns(1)[0]
        with col3:
            st.subheader("Comments by resource_uuid:")
            resource_comments_df = pd.DataFrame.from_dict(
                resource_comments, orient="index"
            ).reset_index()
            resource_comments_df.columns = ["Resource UUID", "Comments"]
            resource_comments_df = resource_comments_df.sort_values(
                by="Comments", ascending=False
            )
            resource_comments_df["Resource UUID"] = (
                "https://yodayo.com/posts/" + resource_comments_df["Resource UUID"]
            )
            st.dataframe(
                resource_comments_df, hide_index=True, column_config=column_config
            )

        col4 = st.columns(1)[0]
        with col4:
            st.subheader("Collected by resource_uuid:")
            resource_collected_df = pd.DataFrame.from_dict(
                resource_collected, orient="index"
            ).reset_index()
            resource_collected_df.columns = ["Resource UUID", "Collected"]
            resource_collected_df = resource_collected_df.sort_values(
                by="Collected", ascending=False
            )
            resource_collected_df["Resource UUID"] = (
                "https://yodayo.com/posts/" + resource_collected_df["Resource UUID"]
            )

            st.dataframe(
                resource_collected_df, hide_index=True, column_config=column_config
            )
            most_collected_resource_uuid = resource_collected_df.iloc[0][
                "Resource UUID"
            ]
            most_collected_count = resource_collected_df.iloc[0]["Collected"]

            st.subheader("Most Collected Post:")
            st.write(f"Post ID: {most_collected_resource_uuid}")
            st.write(f"№ of Collections: {most_collected_count}")
            st.subheader("User Interaction Statistics:")
            st.write(f"№ of Unique Users who Liked: {len(user_likes)}")
            st.write(f"№ of Unique Users who Commented: {len(user_comments)}")
            st.write(f"№ of Users who Collected: {len(resource_collected)}")

        average_likes_per_user = total_likes / len(user_likes)
        st.subheader("Average Likes per User")
        st.write(f"Average Likes per User: {average_likes_per_user:.2f}")
        st.subheader("Percentile:")
        percentiles = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        percentiles_values_likes = np.percentile(likes_df["Likes"], percentiles)
        percentiles_values_comments = np.percentile(
            comments_df["Comments"], percentiles
        )
        st.subheader("% of Likes by Top Users")
        display_top_users_stats(likes_df, 0.05, total_likes)
        display_top_users_stats(likes_df, 0.10, total_likes)
        display_top_users_stats(likes_df, 0.25, total_likes)
        display_top_users_stats(likes_df, 0.50, total_likes)
        col5, col6 = st.columns(2)

        with col5:
            st.subheader("Likes Percentiles")
            for percentile, value in zip(percentiles, percentiles_values_likes):
                rounded_value = round(value, 2)
                st.write(f"{percentile}th percentile: {rounded_value}")

        with col6:
            st.subheader("Comments Percentiles")
            for percentile, value in zip(percentiles, percentiles_values_comments):
                rounded_value = round(value, 2)
                st.write(f"{percentile}th percentile: {rounded_value}")

        likes_df = generate_likes_dataframe(user_likes)
        comments_df = generate_comments_dataframe(
            user_comments, user_is_follower, notifications
        )
        st.subheader("Likes by User:", help="Shows all notifications in order")
        st.dataframe(likes_df, hide_index=True, column_config=column_config)
        st.subheader("Comments by User:")
        query = st.text_input("Search comments by user")
        if query:
            mask = comments_df.applymap(lambda x: query.lower() in str(x).lower()).any(
                axis=1
            )
            filtered_comments_df = comments_df[mask]
        else:
            filtered_comments_df = comments_df
        st.dataframe(filtered_comments_df, hide_index=True, column_config=column_config)
        analyze_likes(user_likes, followers, follower_like_counts)
        end_time = time.perf_counter()
        execution_time = end_time - start_time
        st.write(f"Execution time: {execution_time} seconds")

    else:
        st.warning("Enter your access token and user ID:")


if __name__ == "__main__":
    main()
