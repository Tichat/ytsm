import logging
import mimetypes
import os
from typing import Callable, Union, Any, Optional

from django.contrib.auth.models import User
from django.db import models
from django.db.models.functions import Lower

from YtManagerApp.utils import youtube

# help_text = user shown text
# verbose_name = user shown name
# null = nullable, blank = user is allowed to set value to empty
VIDEO_ORDER_CHOICES = [
    ('newest', 'Newest'),
    ('oldest', 'Oldest'),
    ('playlist', 'Playlist order'),
    ('playlist_reverse', 'Reverse playlist order'),
    ('popularity', 'Popularity'),
    ('rating', 'Top rated'),
]

VIDEO_ORDER_MAPPING = {
    'newest': '-publish_date',
    'oldest': 'publish_date',
    'playlist': 'playlist_index',
    'playlist_reverse': '-playlist_index',
    'popularity': '-views',
    'rating': '-rating'
}


class SubscriptionFolder(models.Model):
    name = models.CharField(null=False, max_length=250)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=False, blank=False)

    class Meta:
        ordering = [Lower('parent__name'), Lower('name')]

    def __str__(self):
        s = ""
        current = self
        while current is not None:
            s = current.name + " > " + s
            current = current.parent
        return s[:-3]

    def __repr__(self):
        return f'folder {self.id}, name="{self.name}"'

    def delete_folder(self, keep_subscriptions: bool):
        if keep_subscriptions:

            def visit(node: Union["SubscriptionFolder", "Subscription"]):
                if isinstance(node, Subscription):
                    node.parent_folder = None
                    node.save()

            SubscriptionFolder.traverse(self.id, self.user, visit)

        self.delete()

    @staticmethod
    def traverse(root_folder_id: Optional[int],
                 user: User,
                 visit_func: Callable[[Union["SubscriptionFolder", "Subscription"]], Any]):

        data_collected = []

        def collect(data):
            if data is not None:
                data_collected.append(data)

        # Visit root
        if root_folder_id is not None:
            root_folder = SubscriptionFolder.objects.get(id=root_folder_id)
            collect(visit_func(root_folder))

        queue = [root_folder_id]
        visited = []

        while len(queue) > 0:
            folder_id = queue.pop()

            if folder_id in visited:
                logging.error('Found folder tree cycle for folder id %d.', folder_id)
                continue
            visited.append(folder_id)

            for folder in SubscriptionFolder.objects.filter(parent_id=folder_id, user=user).order_by(Lower('name')):
                collect(visit_func(folder))
                queue.append(folder.id)

            for subscription in Subscription.objects.filter(parent_folder_id=folder_id, user=user).order_by(Lower('name')):
                collect(visit_func(subscription))

        return data_collected


class Subscription(models.Model):
    name = models.CharField(null=False, max_length=1024)
    parent_folder = models.ForeignKey(SubscriptionFolder, on_delete=models.CASCADE, null=True, blank=True)
    playlist_id = models.CharField(null=False, max_length=128)
    description = models.TextField()
    channel_id = models.CharField(max_length=128)
    channel_name = models.CharField(max_length=1024)
    icon_default = models.CharField(max_length=1024)
    icon_best = models.CharField(max_length=1024)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    # overrides
    auto_download = models.BooleanField(null=True, blank=True)
    download_limit = models.IntegerField(null=True, blank=True)
    download_order = models.CharField(
        null=True, blank=True,
        max_length=128,
        choices=VIDEO_ORDER_CHOICES)
    automatically_delete_watched = models.BooleanField(null=True, blank=True)

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'subscription {self.id}, name="{self.name}", playlist_id="{self.playlist_id}"'

    def fill_from_playlist(self, info_playlist: youtube.Playlist):
        self.name = info_playlist.title
        self.playlist_id = info_playlist.id
        self.description = info_playlist.description
        self.channel_id = info_playlist.channel_id
        self.channel_name = info_playlist.channel_title
        self.icon_default = youtube.default_thumbnail(info_playlist).url
        self.icon_best = youtube.best_thumbnail(info_playlist).url

    def copy_from_channel(self, info_channel: youtube.Channel):
        # No point in storing info about the 'uploads from X' playlist
        self.name = info_channel.title
        self.playlist_id = info_channel.uploads_playlist.id
        self.description = info_channel.description
        self.channel_id = info_channel.id
        self.channel_name = info_channel.title
        self.icon_default = youtube.default_thumbnail(info_channel).url
        self.icon_best = youtube.best_thumbnail(info_channel).url

    def fetch_from_url(self, url, yt_api: youtube.YoutubeAPI):
        url_parsed = yt_api.parse_url(url)
        if 'playlist' in url_parsed:
            info_playlist = yt_api.playlist(url=url)
            if info_playlist is None:
                raise ValueError('Invalid playlist ID!')

            self.fill_from_playlist(info_playlist)
        else:
            info_channel = yt_api.channel(url=url)
            if info_channel is None:
                raise ValueError('Cannot find channel!')

            self.copy_from_channel(info_channel)

    def delete_subscription(self, keep_downloaded_videos: bool):
        self.delete()


class Video(models.Model):
    video_id = models.TextField(null=False)
    name = models.TextField(null=False)
    description = models.TextField()
    watched = models.BooleanField(default=False, null=False)
    downloaded_path = models.TextField(null=True, blank=True)
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE)
    playlist_index = models.IntegerField(null=False)
    publish_date = models.DateTimeField(null=False)
    icon_default = models.TextField()
    icon_best = models.TextField()
    uploader_name = models.TextField(null=False)
    views = models.IntegerField(null=False, default=0)
    rating = models.FloatField(null=False, default=0.5)

    @staticmethod
    def create(playlist_item: youtube.PlaylistItem, subscription: Subscription):
        video = Video()
        video.video_id = playlist_item.resource_video_id
        video.name = playlist_item.title
        video.description = playlist_item.description
        video.watched = False
        video.downloaded_path = None
        video.subscription = subscription
        video.playlist_index = playlist_item.position
        video.publish_date = playlist_item.published_at
        video.icon_default = youtube.default_thumbnail(playlist_item).url
        video.icon_best = youtube.best_thumbnail(playlist_item).url
        video.save()
        return video

    def mark_watched(self):
        self.watched = True
        self.save()
        if self.downloaded_path is not None:
            from YtManagerApp.management.appconfig import appconfig
            from YtManagerApp.management.jobs.delete_video import schedule_delete_video
            from YtManagerApp.management.jobs.synchronize import schedule_synchronize_now_subscription

            if appconfig.for_sub(self.subscription, 'automatically_delete_watched'):
                schedule_delete_video(self)
                schedule_synchronize_now_subscription(self.subscription)

    def mark_unwatched(self):
        from YtManagerApp.management.jobs.synchronize import schedule_synchronize_now_subscription
        self.watched = False
        self.save()
        schedule_synchronize_now_subscription(self.subscription)

    def get_files(self):
        if self.downloaded_path is not None:
            directory, file_pattern = os.path.split(self.downloaded_path)
            for file in os.listdir(directory):
                if file.startswith(file_pattern):
                    yield os.path.join(directory, file)

    def find_video(self):
        """
        Finds the video file from the downloaded files, and
        returns
        :return: Tuple containing file path and mime type
        """
        for file in self.get_files():
            mime, _ = mimetypes.guess_type(file)
            if mime is not None and mime.startswith('video/'):
                return (file, mime)

        return None, None

    def delete_files(self):
        if self.downloaded_path is not None:
            from YtManagerApp.management.jobs.delete_video import schedule_delete_video
            from YtManagerApp.management.appconfig import appconfig
            from YtManagerApp.management.jobs.synchronize import schedule_synchronize_now_subscription

            schedule_delete_video(self)

            # Mark watched?
            if self.subscription.user.preferences['mark_deleted_as_watched']:
                self.watched = True
                schedule_synchronize_now_subscription(self.subscription)

    def download(self):
        if not self.downloaded_path:
            from YtManagerApp.management.jobs.download_video import schedule_download_video
            schedule_download_video(self)

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'video {self.id}, video_id="{self.video_id}"'
